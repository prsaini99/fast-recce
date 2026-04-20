"""OutreachService — list, update, stats for the outreach queue."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.contact import DoNotContact, PropertyContact
from app.models.outreach import OutreachQueue
from app.schemas.outreach import OutreachStats, OutreachUpdate

# Valid status transitions per api-spec.md.
_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"contacted", "declined"},
    "contacted": {"responded", "follow_up", "no_response", "declined"},
    "responded": {"follow_up", "converted", "declined"},
    "follow_up": {"contacted", "converted", "declined", "no_response"},
    "no_response": {"contacted", "follow_up", "declined"},
    "converted": set(),
    "declined": set(),
}


class OutreachService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, outreach_id: UUID) -> OutreachQueue:
        item = await self.db.get(OutreachQueue, outreach_id)
        if item is None:
            raise NotFoundError(f"Outreach item {outreach_id} not found")
        return item

    async def list_items(
        self,
        *,
        statuses: list[str] | None = None,
        city: str | None = None,
        min_priority: int | None = None,
        sort: str = "priority_desc",
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[OutreachQueue], int]:
        from app.models.property import Property

        stmt = select(OutreachQueue)
        filters = []

        if statuses:
            filters.append(OutreachQueue.status.in_(statuses))
        if min_priority is not None:
            filters.append(OutreachQueue.priority >= min_priority)
        if city is not None:
            filters.append(
                OutreachQueue.property_id.in_(
                    select(Property.id).where(Property.city == city)
                )
            )

        count_stmt = select(func.count(OutreachQueue.id))
        if filters:
            count_stmt = count_stmt.where(*filters)
        total = int((await self.db.execute(count_stmt)).scalar_one())

        if filters:
            stmt = stmt.where(*filters)
        stmt = _apply_sort(stmt, sort).offset(offset).limit(limit)
        rows = list((await self.db.execute(stmt)).scalars().all())
        return rows, total

    async def update(
        self, outreach_id: UUID, data: OutreachUpdate
    ) -> OutreachQueue:
        item = await self.get(outreach_id)

        if data.status is not None and data.status != item.status:
            allowed = _TRANSITIONS.get(item.status, set())
            if data.status not in allowed:
                raise ConflictError(
                    f"invalid outreach status transition: {item.status} -> {data.status} "
                    f"(allowed: {sorted(allowed) or '(none — terminal)'})"
                )
            if data.status == "contacted":
                await self._assert_not_dnc(item.property_id)
                item.contact_attempts += 1
                now = datetime.now(UTC)
                if item.first_contact_at is None:
                    item.first_contact_at = now
                item.last_contact_at = now
            item.status = data.status

        if data.priority is not None:
            item.priority = data.priority
        if data.outreach_channel is not None:
            item.outreach_channel = data.outreach_channel
        if data.follow_up_at is not None:
            item.follow_up_at = data.follow_up_at
        if data.notes is not None:
            item.notes = data.notes

        await self.db.flush()
        await self.db.refresh(item)
        return item

    async def stats(self, city: str | None = None) -> OutreachStats:
        from app.models.property import Property

        status_stmt = select(
            OutreachQueue.status, func.count(OutreachQueue.id)
        ).group_by(OutreachQueue.status)
        if city is not None:
            status_stmt = status_stmt.where(
                OutreachQueue.property_id.in_(
                    select(Property.id).where(Property.city == city)
                )
            )

        by_status = {
            row[0]: int(row[1])
            for row in (await self.db.execute(status_stmt)).all()
        }

        total = sum(by_status.values())
        converted = by_status.get("converted", 0)
        conversion_rate = (converted / total) if total else 0.0

        attempts_stmt = select(func.coalesce(func.avg(OutreachQueue.contact_attempts), 0.0))
        if city is not None:
            attempts_stmt = attempts_stmt.where(
                OutreachQueue.property_id.in_(
                    select(Property.id).where(Property.city == city)
                )
            )
        avg_attempts = float(
            (await self.db.execute(attempts_stmt)).scalar_one() or 0.0
        )

        return OutreachStats(
            total=total,
            by_status=by_status,
            conversion_rate=round(conversion_rate, 3),
            avg_contact_attempts=round(avg_attempts, 2),
        )

    # --- Internals ---

    async def _assert_not_dnc(self, property_id: UUID) -> None:
        contact_stmt = select(PropertyContact).where(
            PropertyContact.property_id == property_id
        )
        contacts = list((await self.db.execute(contact_stmt)).scalars().all())
        if not contacts:
            return

        for contact in contacts:
            dnc_stmt = select(DoNotContact).where(
                DoNotContact.contact_type == contact.contact_type,
                DoNotContact.contact_value == contact.normalized_value,
            )
            if (await self.db.execute(dnc_stmt)).scalar_one_or_none() is not None:
                raise ValidationError(
                    f"cannot mark as contacted: {contact.contact_type} "
                    f"'{contact.contact_value}' is on the do-not-contact list"
                )


def _apply_sort(stmt, sort: str):  # type: ignore[no-untyped-def]
    match sort:
        case "priority_desc":
            return stmt.order_by(
                OutreachQueue.priority.desc(), OutreachQueue.created_at.asc()
            )
        case "created_at_desc":
            return stmt.order_by(OutreachQueue.created_at.desc())
        case "follow_up_at_asc":
            return stmt.order_by(OutreachQueue.follow_up_at.asc().nulls_last())
        case "last_contact_at_desc":
            return stmt.order_by(OutreachQueue.last_contact_at.desc().nulls_last())
        case _:
            return stmt.order_by(OutreachQueue.created_at.desc())
