"""Public search router — user-facing, no auth.

POST /search is async: it creates a SearchJob row, fires a background task
to run the actual scrape, and returns immediately so the browser can poll
GET /search/jobs/{id}. That way a page refresh during the 30-60s scrape
doesn't re-run it — the job keeps going and the refreshed page picks it
back up via the polling endpoint.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_db,
    get_property_service,
    get_search_history_service,
    get_search_job_service,
)
from app.schemas.property import PropertyDetail, PropertyRead
from app.schemas.search import SearchRequest, SearchResponse, SearchResultItem
from app.schemas.search_history import SearchHistoryItem, SearchHistorySuggestion
from app.schemas.search_job import SearchJobDetail, SearchJobRead
from app.services.property_service import PropertyService
from app.services.search_history_service import (
    SearchHistoryService,
    normalize_query,
)
from app.services.search_job_service import (
    SearchJobService,
    cancel_task,
    spawn_search_job,
)

router = APIRouter(prefix="/api/v1/search", tags=["search"])


@router.post("", response_model=SearchJobRead, status_code=202)
async def start_search(
    request: SearchRequest,
    db: AsyncSession = Depends(get_db),
    jobs: SearchJobService = Depends(get_search_job_service),
    history: SearchHistoryService = Depends(get_search_history_service),
) -> SearchJobRead:
    """Kick off (or coalesce to) a search job and return its record.

    - Cache hit (completed search_history exists): creates a job already in
      the `completed` state so clients can immediately fetch the response.
    - Running duplicate: returns the existing in-flight job so two
      simultaneous submissions of the same query don't double-scrape.
    - Cache miss: creates a new running job + schedules the scrape on the
      event loop.
    """
    normalized = normalize_query(request.query)

    # 1. Cache hit → create a completed job linked to the existing history.
    #    Skipped when the client explicitly asked to refresh (the "Find more
    #    results" button) — we still want to re-scrape even if we have a
    #    cached history row, and SearchService will union the new IDs into it.
    if not request.refresh:
        cached_history = await history.get_by_normalized(normalized)
        if cached_history is not None:
            job = await jobs.create(
                query_text=request.query,
                normalized_query=normalized,
                max_results=request.max_results,
                status="completed",
                search_history_id=cached_history.id,
            )
            await db.commit()
            return SearchJobRead.model_validate(job)

    # 2. Already running for this exact query → hand back the same job.
    running = await jobs.find_running_for_query(normalized)
    if running is not None:
        return SearchJobRead.model_validate(running)

    # 3. Fresh scrape — spawn a background task with its own session.
    job = await jobs.create(
        query_text=request.query,
        normalized_query=normalized,
        max_results=request.max_results,
        status="running",
    )
    await db.commit()
    # Schedule after commit so the task can see the row.
    spawn_search_job(job.id, request)
    return SearchJobRead.model_validate(job)


@router.delete("/jobs/{job_id}", response_model=SearchJobRead)
async def cancel_search_job(
    job_id: UUID,
    jobs: SearchJobService = Depends(get_search_job_service),
) -> SearchJobRead:
    """Cancel a running job and release its resources.

    - Calls `asyncio.Task.cancel()` on the background scrape task, which
      propagates `CancelledError` up through the pipeline. That unwinds
      the `async with SessionLocal()` so the Postgres connection is
      returned to the pool (no more phantom 'idle in transaction' rows).
    - Marks the DB row `failed` with `error='cancelled by user'` so the
      sidebar drops it from the Running list on the next poll.

    If the task has already finished between the list fetch and the
    cancel click, we still flip the row state for idempotency.
    """
    from app.exceptions import NotFoundError

    job = await jobs.get(job_id)
    if job is None:
        raise NotFoundError(f"search job {job_id} not found")

    if job.status == "running":
        # Ask the asyncio task to stop. The task's cancel handler marks
        # the row failed on its way out, but we also mark it here
        # synchronously so the API response reflects the new state
        # immediately (the task cleanup is eventually consistent).
        cancel_task(job_id)
        await jobs.mark_failed(job_id, "cancelled by user")

    job = await jobs.get(job_id)
    assert job is not None
    return SearchJobRead.model_validate(job)


@router.get("/jobs", response_model=list[SearchJobRead])
async def list_search_jobs(
    status: str | None = Query(
        default=None, description="running | completed | failed"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    jobs: SearchJobService = Depends(get_search_job_service),
) -> list[SearchJobRead]:
    """List jobs — pass `status=running` to drive the running-jobs sidebar."""
    rows = await jobs.list(status=status, limit=limit)
    return [SearchJobRead.model_validate(r) for r in rows]


@router.get("/jobs/{job_id}", response_model=SearchJobDetail)
async def get_search_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    jobs: SearchJobService = Depends(get_search_job_service),
    property_service: PropertyService = Depends(get_property_service),
) -> SearchJobDetail:
    """Poll a single job. When completed, the `response` field is filled."""
    job = await jobs.get(job_id)
    if job is None:
        from app.exceptions import NotFoundError

        raise NotFoundError(f"search job {job_id} not found")

    detail = SearchJobDetail.model_validate(job)
    if job.status == "completed" and job.search_history_id is not None:
        detail.response = await _build_response_from_history(
            db, job.search_history_id, job.query_text, job.max_results,
            property_service=property_service,
        )
    return detail


@router.get("/history", response_model=list[SearchHistoryItem])
async def list_search_history(
    limit: int = Query(default=50, ge=1, le=200),
    history: SearchHistoryService = Depends(get_search_history_service),
) -> list[SearchHistoryItem]:
    rows = await history.list_recent(limit=limit)
    return [
        SearchHistoryItem.model_validate(SearchHistoryService.to_item_dict(r))
        for r in rows
    ]


@router.get("/history/suggest", response_model=list[SearchHistorySuggestion])
async def suggest_search_history(
    q: str = Query(min_length=1, max_length=300),
    limit: int = Query(default=8, ge=1, le=20),
    history: SearchHistoryService = Depends(get_search_history_service),
) -> list[SearchHistorySuggestion]:
    rows = await history.suggest(prefix=q, limit=limit)
    return [
        SearchHistorySuggestion(
            query_text=r.query_text,
            result_count=len(r.result_property_ids or []),
            last_searched_at=r.last_searched_at,
        )
        for r in rows
    ]


@router.get("/property/{property_id}", response_model=PropertyDetail)
async def get_public_property(
    property_id: UUID,
    service: PropertyService = Depends(get_property_service),
) -> PropertyDetail:
    prop, contacts, _outreach_ignored = await service.get_detail(property_id)

    base = PropertyRead.model_validate(prop).model_dump()
    return PropertyDetail(
        **base,
        contacts=[
            {
                "id": str(c.id),
                "contact_type": c.contact_type,
                "contact_value": c.contact_value,
                "normalized_value": c.normalized_value,
                "source_name": c.source_name,
                "source_url": c.source_url,
                "extraction_method": c.extraction_method,
                "confidence": c.confidence,
                "is_public_business_contact": c.is_public_business_contact,
                "is_primary": c.is_primary,
                "flagged_personal": c.flagged_personal,
            }
            for c in contacts
        ],
        outreach=None,
    )


# --- Internals ---


async def _build_response_from_history(
    db: AsyncSession,
    history_id: UUID,
    query_text: str,
    max_results: int,
    *,
    property_service: PropertyService,
) -> SearchResponse | None:
    """Reconstruct the SearchResponse payload from a completed search_history row.

    Also annotates each result with `source_query_*` when the property was
    first discovered by an older query — same behaviour as the synchronous
    path used to have inline.
    """
    from app.models.search_history import SearchHistory
    from app.services.search_history_service import SearchHistoryService

    history_row = await db.get(SearchHistory, history_id)
    if history_row is None:
        return None

    ids = []
    for raw in history_row.result_property_ids or []:
        try:
            ids.append(UUID(str(raw)))
        except (ValueError, TypeError):
            continue
    rows = await property_service.list_by_ids(ids)
    first_sources = await SearchHistoryService(db=db).first_search_by_property(
        [r.id for r in rows]
    )

    from app.services.search_service import SearchService

    results: list[SearchResultItem] = []
    normalized = normalize_query(query_text)
    for row in rows[:max_results]:
        item = SearchService._to_result_item(row)
        source = first_sources.get(row.id)
        if source is not None and source.normalized_query != normalized:
            item.source_query_id = source.id
            item.source_query_text = source.query_text
        results.append(item)

    return SearchResponse(
        query=query_text,
        inferred_city=history_row.inferred_city,
        inferred_property_type=history_row.inferred_property_type,
        results=results,
        candidates_discovered=0,
        candidates_new=0,
        candidates_skipped_known=len(rows),
        candidates_filtered_non_shoot=0,
        airbnb_listings_scraped=0,
        magicbricks_listings_scraped=0,
        acres99_listings_scraped=0,
        duration_seconds=history_row.duration_seconds or 0.0,
        errors=[],
    )
