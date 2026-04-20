"""Delete properties not linked to any search_history row.

Run once after shipping the "synced-only Leads" change to clear out the
legacy rows produced by the daily discovery pipeline that no user ever
searched for. `property_contacts` and `outreach_queue` rows are dropped
transitively via ON DELETE CASCADE; `discovery_candidates` are untouched
(pipeline state, not user-facing data).

Usage:
    cd backend
    .venv/bin/python -m scripts.cleanup_orphan_properties [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import delete, select

from app.database import SessionLocal
from app.models import search_history  # noqa: F401 — register metadata
from app.models.property import Property
from app.services.search_history_service import SearchHistoryService


async def main(dry_run: bool) -> None:
    async with SessionLocal() as db:
        owned = await SearchHistoryService(db=db).owned_property_ids()
        all_ids = set(
            (await db.execute(select(Property.id))).scalars().all()
        )
        orphan_ids: list[UUID] = sorted(all_ids - owned)

        print(
            f"Total properties:       {len(all_ids)}\n"
            f"Linked to a search:     {len(owned & all_ids)}\n"
            f"Orphans (to delete):    {len(orphan_ids)}"
        )

        if not orphan_ids:
            print("Nothing to do.")
            return

        if dry_run:
            print("[dry-run] Pass without --dry-run to actually delete.")
            return

        # Delete in batches so a single huge IN clause doesn't blow up.
        BATCH = 500
        deleted = 0
        for i in range(0, len(orphan_ids), BATCH):
            batch = orphan_ids[i : i + BATCH]
            result = await db.execute(
                delete(Property).where(Property.id.in_(batch))
            )
            deleted += result.rowcount or 0
        await db.commit()
        print(f"Deleted {deleted} orphan properties.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
