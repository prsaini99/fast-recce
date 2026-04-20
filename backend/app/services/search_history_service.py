"""SearchHistoryService — persist, look up, suggest past user searches.

Every call to the user-facing search passes through here:
  1. `get_by_normalized()` — cache hit check before running the pipeline.
  2. `record()` — upsert after the pipeline runs (or bump counters on hit).
  3. `list_recent()` — sidebar of past searches.
  4. `suggest()` — prefix autocomplete on the search input.
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.search_history import SearchHistory

_WS_RE = re.compile(r"\s+")


def normalize_query(q: str) -> str:
    """Lowercase + trim + collapse whitespace. Used for cache keying."""
    return _WS_RE.sub(" ", q.strip().lower())


class SearchHistoryService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_normalized(self, normalized: str) -> SearchHistory | None:
        stmt = select(SearchHistory).where(
            SearchHistory.normalized_query == normalized
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def record(
        self,
        *,
        query_text: str,
        normalized_query: str,
        inferred_city: str | None,
        inferred_property_type: str | None,
        result_property_ids: list[UUID],
        duration_seconds: float | None,
    ) -> SearchHistory:
        """Create a new history row or bump the existing one's counters.

        On cache-hit we update `last_searched_at` + `search_count`; we
        deliberately *don't* overwrite the stored result IDs. The result
        set belongs to the run that first populated it; refreshing it
        should be an explicit action, not a side effect of a re-search.
        """
        existing = await self.get_by_normalized(normalized_query)
        if existing is not None:
            existing.search_count += 1
            # `onupdate=func.now()` on the column takes care of
            # last_searched_at when we flush.

            # Union: "Find more results" retriggers the pipeline for a
            # query we've already scraped. New property IDs need to be
            # appended to the history row so the cached replay sees them
            # on subsequent reads. We preserve insertion order (oldest
            # first) and dedupe via a seen-set.
            if result_property_ids:
                seen: set[str] = set()
                merged: list[str] = []
                for pid in list(existing.result_property_ids or []) + [
                    str(p) for p in result_property_ids
                ]:
                    key = str(pid)
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(key)
                existing.result_property_ids = merged
            await self.db.flush()
            await self.db.refresh(existing)
            return existing

        row = SearchHistory(
            query_text=query_text,
            normalized_query=normalized_query,
            inferred_city=inferred_city,
            inferred_property_type=inferred_property_type,
            result_property_ids=[str(pid) for pid in result_property_ids],
            duration_seconds=duration_seconds,
        )
        self.db.add(row)
        await self.db.flush()
        await self.db.refresh(row)
        return row

    async def owned_property_ids(self) -> set[UUID]:
        """Return every property_id referenced by any search_history row.

        Used by the Leads list to restrict results to "synced" properties —
        those that were surfaced by a user search at least once. Properties
        persisted by the daily discovery pipeline that no one ever searched
        for are excluded.
        """
        stmt = select(SearchHistory.result_property_ids)
        rows = (await self.db.execute(stmt)).scalars().all()
        owned: set[UUID] = set()
        for arr in rows:
            if not arr:
                continue
            for raw in arr:
                try:
                    owned.add(UUID(str(raw)))
                except (ValueError, TypeError):
                    continue
        return owned

    async def first_search_by_property(
        self, property_ids: list[UUID]
    ) -> dict[UUID, SearchHistory]:
        """Map property_id → the earliest search_history row that listed it.

        Lets the UI render "first seen in '<past query>'" badges: when a new
        query surfaces a property that an older query already discovered, we
        link the card back to that older query instead of re-claiming the
        property for the current search.
        """
        if not property_ids:
            return {}
        wanted = {pid for pid in property_ids}
        stmt = select(SearchHistory).order_by(
            SearchHistory.first_searched_at.asc()
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        out: dict[UUID, SearchHistory] = {}
        for row in rows:
            if not row.result_property_ids:
                continue
            for raw in row.result_property_ids:
                try:
                    pid = UUID(str(raw))
                except (ValueError, TypeError):
                    continue
                if pid in wanted and pid not in out:
                    out[pid] = row
            if len(out) == len(wanted):
                break
        return out

    async def list_recent(self, limit: int = 50) -> list[SearchHistory]:
        stmt = (
            select(SearchHistory)
            .order_by(SearchHistory.last_searched_at.desc())
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def suggest(self, prefix: str, limit: int = 8) -> list[SearchHistory]:
        """Return past queries whose normalized form starts with `prefix`.

        Case-insensitive (prefix is normalized). Returns most-recently-used
        first so users' common searches bubble to the top.
        """
        needle = normalize_query(prefix)
        if not needle:
            return []
        stmt = (
            select(SearchHistory)
            .where(SearchHistory.normalized_query.like(f"{needle}%"))
            .order_by(
                SearchHistory.search_count.desc(),
                SearchHistory.last_searched_at.desc(),
            )
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    @staticmethod
    def to_item_dict(row: SearchHistory) -> dict[str, object]:
        ids = row.result_property_ids or []
        return {
            "id": row.id,
            "query_text": row.query_text,
            "inferred_city": row.inferred_city,
            "inferred_property_type": row.inferred_property_type,
            "result_count": len(ids),
            "search_count": row.search_count,
            "last_searched_at": row.last_searched_at,
        }
