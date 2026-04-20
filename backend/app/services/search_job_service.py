"""SearchJobService — durable async job queue for user searches.

Keeps long-running scrapes off the HTTP request path: POST /search creates
a SearchJob row, spawns an asyncio background task that runs the actual
search with its own DB session + integrations, and returns immediately
with the job id. Clients poll GET /search/jobs/{id} until status flips
from 'running' to 'completed' or 'failed'.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

# Hard ceiling for a single background scrape. If the pipeline hasn't
# returned by this deadline we cancel the task and mark the job failed
# so the Running sidebar doesn't fill up with dead entries.
#
# Ten minutes gives headroom for Gemini free-tier rate limits — a single
# `max_results=10` search can make ~20 LLM calls end-to-end (score + brief
# per candidate) and each call can take 5-15s. Shorter timeouts killed
# legitimate scrapes mid-way through the scoring phase.
_JOB_TIMEOUT_SECONDS = 600.0

# In-flight asyncio.Task registry, keyed by search_job.id. Lets the cancel
# endpoint call `task.cancel()` so the background scrape actually stops
# and its `async with SessionLocal()` releases the DB connection — rather
# than only flipping the DB row state while the task keeps running.
_ACTIVE_TASKS: dict[UUID, "asyncio.Task[None]"] = {}


def cancel_task(job_id: UUID) -> bool:
    """Cancel the running asyncio task for `job_id`, if any.

    Returns True if a task was found and `cancel()` was called (the task
    may still run briefly while unwinding). Safe to call concurrently
    with the task's natural completion — asyncio dedupes.
    """
    task = _ACTIVE_TASKS.get(job_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models.search_job import SearchJob
from app.schemas.search import SearchRequest
from app.services.search_history_service import normalize_query

logger = logging.getLogger(__name__)


class SearchJobService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, job_id: UUID) -> SearchJob | None:
        return await self.db.get(SearchJob, job_id)

    async def list(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[SearchJob]:
        stmt = select(SearchJob).order_by(SearchJob.started_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(SearchJob.status == status)
        return list((await self.db.execute(stmt)).scalars().all())

    async def find_running_for_query(self, normalized: str) -> SearchJob | None:
        """Coalesce concurrent submissions of the same query.

        Two browser tabs hitting POST /search with the same text should
        share one scrape, not race two. Returns the existing running job
        if present so callers can hand its id back to the client instead
        of spawning a duplicate.
        """
        stmt = (
            select(SearchJob)
            .where(
                SearchJob.normalized_query == normalized,
                SearchJob.status == "running",
            )
            .order_by(SearchJob.started_at.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        query_text: str,
        normalized_query: str,
        max_results: int,
        status: str = "running",
        search_history_id: UUID | None = None,
        error: str | None = None,
    ) -> SearchJob:
        finished = datetime.now(UTC) if status != "running" else None
        job = SearchJob(
            query_text=query_text,
            normalized_query=normalized_query,
            status=status,
            max_results=max_results,
            search_history_id=search_history_id,
            error=error,
            finished_at=finished,
        )
        self.db.add(job)
        await self.db.flush()
        await self.db.refresh(job)
        return job

    async def mark_completed(
        self, job_id: UUID, search_history_id: UUID | None
    ) -> None:
        job = await self.db.get(SearchJob, job_id)
        if job is None:
            return
        job.status = "completed"
        job.search_history_id = search_history_id
        job.finished_at = datetime.now(UTC)
        await self.db.flush()

    async def mark_failed(self, job_id: UUID, error: str) -> None:
        job = await self.db.get(SearchJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.error = error[:2000]
        job.finished_at = datetime.now(UTC)
        await self.db.flush()


def spawn_search_job(
    job_id: UUID,
    request: SearchRequest,
) -> None:
    """Fire-and-forget a scrape task for `job_id` onto the running event loop.

    The task owns its own AsyncSession + integrations so it's decoupled from
    the HTTP request that scheduled it. That way the HTTP response is sent
    immediately and the scrape continues to run even if the client aborts
    (refresh, close tab, disconnect).

    The task is registered in `_ACTIVE_TASKS` so the cancel endpoint can
    reach it; a done-callback pops the entry once the task finishes so
    the dict doesn't leak completed tasks.
    """
    loop = asyncio.get_event_loop()
    task = loop.create_task(_run_search_job(job_id, request))
    _ACTIVE_TASKS[job_id] = task

    def _cleanup(_t: "asyncio.Task[None]") -> None:
        _ACTIVE_TASKS.pop(job_id, None)

    task.add_done_callback(_cleanup)


async def _run_search_job(job_id: UUID, request: SearchRequest) -> None:
    """Body of the background task. Must not raise — errors go to `error`."""
    try:
        await asyncio.wait_for(
            _run_search_job_inner(job_id, request),
            timeout=_JOB_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        # Explicit cancel from the cancel endpoint. The inner task's
        # `async with SessionLocal()` has already closed + released the
        # DB connection on its way out; we just need to record the
        # terminal state so the UI reflects it and the row isn't stuck
        # as 'running' forever.
        logger.info("search job %s cancelled", job_id)
        try:
            async with SessionLocal() as db:
                await SearchJobService(db=db).mark_failed(
                    job_id, "cancelled by user"
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("failed to mark job %s cancelled", job_id)
        # Do NOT re-raise — task should exit cleanly so the done-callback
        # pops it from `_ACTIVE_TASKS`.
    except TimeoutError:
        logger.warning(
            "search job %s exceeded %.0fs timeout",
            job_id,
            _JOB_TIMEOUT_SECONDS,
        )
        try:
            async with SessionLocal() as db:
                await SearchJobService(db=db).mark_failed(
                    job_id,
                    f"timed out after {int(_JOB_TIMEOUT_SECONDS)}s",
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("failed to mark job %s failed (timeout)", job_id)
    except Exception as exc:  # noqa: BLE001 — we want to capture ANY failure
        logger.exception("search job %s failed", job_id)
        try:
            async with SessionLocal() as db:
                await SearchJobService(db=db).mark_failed(job_id, str(exc))
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("failed to mark job %s failed", job_id)


async def _run_search_job_inner(job_id: UUID, request: SearchRequest) -> None:
    # Import here to avoid a circular dependency: SearchService pulls in
    # SearchHistoryService which is referenced from this module's deps.
    from app.api.deps import build_search_service

    async with SessionLocal() as db:
        async with build_search_service(db) as search_service:
            await search_service.search(request)
        # `search()` commits its own writes, including the new
        # search_history row. Look it up to link to the job.
        history_id = await _find_history_id(db, request.query)
        job_service = SearchJobService(db=db)
        await job_service.mark_completed(job_id, history_id)
        await db.commit()


async def _find_history_id(db: AsyncSession, query_text: str) -> UUID | None:
    from app.models.search_history import SearchHistory

    normalized = normalize_query(query_text)
    stmt = (
        select(SearchHistory.id)
        .where(SearchHistory.normalized_query == normalized)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
