"""PropertyService — canonical-entity CRUD used across the pipeline.

Scope for now: just what M5 needs to promote a discovery candidate into a
property record. Full read/list/review APIs come with M9 (dashboard).
Dedup is M6's concern — for now every candidate becomes a new property.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


def uuid_nil() -> UUID:
    """Placeholder UUID that is guaranteed not to match any real row.

    Lets us shove a filter of `Property.id == uuid_nil()` into the query
    when the caller passed `only_ids=[]`, producing a valid SQL statement
    with zero matches rather than blowing up on an empty IN clause.
    """
    return UUID("00000000-0000-0000-0000-000000000000")

logger = logging.getLogger(__name__)

from app.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.contact import DoNotContact, PropertyContact
from app.models.outreach import OutreachQueue
from app.models.property import Property
from app.schemas.property import PropertyUpsertFromCandidate
from app.schemas.review import ReviewRequest, ReviewResponse


class PropertyService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, property_id: UUID) -> Property:
        prop = await self.db.get(Property, property_id)
        if prop is None:
            raise NotFoundError(f"Property {property_id} not found")
        return prop

    async def find_by_google_place_id(self, place_id: str) -> Property | None:
        # Use ORDER BY + first() instead of scalar_one_or_none() so duplicate
        # rows (data cruft from earlier failed ingest attempts; the column
        # has no UNIQUE constraint yet) don't crash the upsert. Pick the
        # oldest row deterministically and log a warning so we can clean
        # the duplicates up later.
        stmt = (
            select(Property)
            .where(Property.google_place_id == place_id)
            .order_by(Property.created_at.asc())
        )
        result = await self.db.execute(stmt)
        rows = result.scalars().all()
        if len(rows) > 1:
            logger.warning(
                "Duplicate properties for google_place_id=%s (count=%d, ids=%s)",
                place_id, len(rows), [str(r.id) for r in rows],
            )
        return rows[0] if rows else None

    async def upsert_from_candidate(
        self, data: PropertyUpsertFromCandidate
    ) -> Property:
        """Create-or-update a property based on a discovery candidate.

        Dedup is intentionally minimal here — only an exact google_place_id
        match. M6 will replace this with multi-signal dedup (phone, geo,
        name similarity, image hash).
        """
        existing = (
            await self.find_by_google_place_id(data.google_place_id)
            if data.google_place_id
            else None
        )

        if existing is not None:
            self._apply_candidate_fields(existing, data)
            await self.db.flush()
            await self.db.refresh(existing)
            return existing

        prop = Property(
            canonical_name=data.canonical_name,
            normalized_name=normalize_name(data.canonical_name),
            normalized_address=None,
            city=data.city,
            locality=data.locality,
            state=data.state,
            pincode=data.pincode,
            lat=data.lat,
            lng=data.lng,
            location=_geography_point(data.lat, data.lng),
            property_type=data.property_type,
            status="new",
            canonical_website=data.website,
            google_place_id=data.google_place_id,
            google_rating=data.google_rating,
            google_review_count=data.google_review_count,
            features_json=data.features_json,
        )
        self.db.add(prop)
        await self.db.flush()
        await self.db.refresh(prop)
        return prop

    async def update_canonical_contacts(
        self,
        property_id: UUID,
        *,
        phone: str | None = None,
        email: str | None = None,
        website: str | None = None,
    ) -> Property:
        prop = await self.get(property_id)
        if phone is not None:
            prop.canonical_phone = phone
        if email is not None:
            prop.canonical_email = email
        if website is not None:
            prop.canonical_website = website
        await self.db.flush()
        await self.db.refresh(prop)
        return prop

    async def merge_features(
        self, property_id: UUID, features: dict[str, object]
    ) -> Property:
        """Merge a CrawlResult's features dict into properties.features_json."""
        prop = await self.get(property_id)
        merged = {**(prop.features_json or {}), **features}
        prop.features_json = merged
        await self.db.flush()
        await self.db.refresh(prop)
        return prop

    # --- Public search read (product pivot) ---

    async def list_by_ids(
        self,
        ids: list[UUID],
        *,
        include_duplicates: bool = False,
    ) -> list[Property]:
        """Return properties with the given IDs, preserving caller order.

        Used by `SearchService` to surface freshly-scraped Airbnb listings
        whose `city` field (set from Airbnb's metadata) doesn't match the
        user's free-text location hint — e.g. user typed "kandivali" but
        Airbnb tagged the listing with `city="Mumbai"`. The fuzzy
        location-hint search would never find them; loading by ID does.
        """
        if not ids:
            return []
        stmt = select(Property).where(Property.id.in_(ids))
        if not include_duplicates:
            stmt = stmt.where(Property.is_duplicate.is_(False))
        rows = (await self.db.execute(stmt)).scalars().all()
        # Re-order to match the input order (DB returns whatever).
        by_id = {r.id: r for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    async def find_by_location_hint(
        self,
        *,
        city_hint: str,
        limit: int = 10,
        include_duplicates: bool = False,
        property_types: list[str] | None = None,
    ) -> list[Property]:
        """Return non-duplicate properties whose location matches the hint.

        Google Places often assigns narrow sub-localities (e.g. 'Chaul',
        'Nagaon') to properties in the Alibaug district. Exact matching on
        `city` misses them. This method ALSO matches when the hint appears
        in `locality` or `canonical_name`. Ranked by relevance_score DESC.

        `property_types` (optional): when provided, restrict results to
        rows whose `property_type` is in the given set. Used by the public
        search route so that e.g. a "property in kandivali" query (generic
        route → residential intent) doesn't surface cached cafes/restaurants
        from earlier "cafe in kandivali" searches.
        """
        pattern = f"%{city_hint.lower()}%"
        hint_filter = (
            (func.lower(Property.city).like(pattern))
            | (func.lower(Property.locality).like(pattern))
            | (func.lower(Property.canonical_name).like(pattern))
        )
        stmt = (
            select(Property)
            .where(hint_filter)
            .order_by(
                Property.relevance_score.desc().nulls_last(),
                Property.created_at.desc(),
            )
            .limit(limit)
        )
        if not include_duplicates:
            stmt = stmt.where(Property.is_duplicate.is_(False))
        if property_types:
            stmt = stmt.where(Property.property_type.in_(property_types))
        rows = (await self.db.execute(stmt)).scalars().all()
        return list(rows)

    # --- Dashboard read (M9) ---

    async def list_for_dashboard(
        self,
        *,
        city: str | None = None,
        property_types: list[str] | None = None,
        statuses: list[str] | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        has_phone: bool | None = None,
        has_email: bool | None = None,
        include_duplicates: bool = False,
        search: str | None = None,
        only_ids: list[UUID] | None = None,
        sort: str = "relevance_score_desc",
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Property], int]:
        """List properties for the Lead Queue. Returns (items, total_count)."""
        filters = []
        if city is not None:
            filters.append(Property.city == city)
        if property_types:
            filters.append(Property.property_type.in_(property_types))
        if statuses:
            filters.append(Property.status.in_(statuses))
        if min_score is not None:
            filters.append(Property.relevance_score >= min_score)
        if max_score is not None:
            filters.append(Property.relevance_score <= max_score)
        if has_phone is True:
            filters.append(Property.canonical_phone.is_not(None))
        elif has_phone is False:
            filters.append(Property.canonical_phone.is_(None))
        if has_email is True:
            filters.append(Property.canonical_email.is_not(None))
        elif has_email is False:
            filters.append(Property.canonical_email.is_(None))
        if not include_duplicates:
            filters.append(Property.is_duplicate.is_(False))
        if search:
            pattern = f"%{search.lower()}%"
            filters.append(
                (func.lower(Property.canonical_name).like(pattern))
                | (func.lower(Property.locality).like(pattern))
                | (func.lower(Property.city).like(pattern))
            )
        if only_ids is not None:
            # Empty list = no rows; SQL IN () would be a syntax error, so
            # force a guaranteed-empty filter instead.
            if not only_ids:
                filters.append(Property.id == uuid_nil())
            else:
                filters.append(Property.id.in_(only_ids))

        # Count first.
        count_stmt = select(func.count(Property.id))
        if filters:
            count_stmt = count_stmt.where(*filters)
        total = (await self.db.execute(count_stmt)).scalar_one()

        # Page.
        stmt = select(Property)
        if filters:
            stmt = stmt.where(*filters)
        stmt = _apply_sort(stmt, sort).offset(offset).limit(limit)
        rows = list((await self.db.execute(stmt)).scalars().all())
        return rows, int(total)

    async def get_detail(self, property_id: UUID) -> tuple[Property, list[PropertyContact], OutreachQueue | None]:
        """Load the property + related rows the dashboard detail view needs."""
        prop = await self.get(property_id)

        contacts_stmt = (
            select(PropertyContact)
            .where(PropertyContact.property_id == property_id)
            .order_by(PropertyContact.confidence.desc())
        )
        contacts = list((await self.db.execute(contacts_stmt)).scalars().all())

        outreach_stmt = select(OutreachQueue).where(OutreachQueue.property_id == property_id)
        outreach = (await self.db.execute(outreach_stmt)).scalar_one_or_none()

        return prop, contacts, outreach

    # --- Review actions (M9) ---

    async def review(
        self, property_id: UUID, request: ReviewRequest
    ) -> ReviewResponse:
        """Apply a review action. Enforces status transitions + side effects."""
        prop = await self.get(property_id)
        action = request.action

        if action == "approve":
            _assert_transition(prop.status, {"new", "reviewed", "rejected"}, action)
            prop.status = "approved"
            created = await self._ensure_outreach_entry(prop)
            return ReviewResponse(
                property_id=property_id,
                status=prop.status,
                action_applied=action,
                outreach_created=created,
            )

        if action == "reject":
            _assert_transition(
                prop.status, {"new", "reviewed", "approved"}, action
            )
            prop.status = "rejected"
            return ReviewResponse(
                property_id=property_id, status=prop.status, action_applied=action
            )

        if action == "reopen":
            _assert_transition(prop.status, {"rejected", "do_not_contact"}, action)
            prop.status = "new"
            return ReviewResponse(
                property_id=property_id, status=prop.status, action_applied=action
            )

        if action == "do_not_contact":
            prop.status = "do_not_contact"
            added = await self._blocklist_contacts(property_id, request.notes or "dnc via review")
            return ReviewResponse(
                property_id=property_id,
                status=prop.status,
                action_applied=action,
                dnc_entries_added=added,
            )

        if action == "merge":
            if request.merge_into_id is None:
                raise ValidationError("merge action requires merge_into_id")
            if request.merge_into_id == property_id:
                raise ValidationError("cannot merge a property into itself")
            # Ensure target exists. Actual contact-move is DedupService's job.
            await self.get(request.merge_into_id)
            prop.status = "reviewed"
            prop.is_duplicate = True
            prop.duplicate_of = request.merge_into_id
            return ReviewResponse(
                property_id=property_id,
                status=prop.status,
                action_applied=action,
                merged_into_id=request.merge_into_id,
            )

        raise ValidationError(f"unsupported review action: {action}")

    # --- Internal helpers ---

    async def _ensure_outreach_entry(self, prop: Property) -> bool:
        """Create an outreach queue entry for a newly-approved property."""
        existing_stmt = select(OutreachQueue).where(OutreachQueue.property_id == prop.id)
        existing = (await self.db.execute(existing_stmt)).scalar_one_or_none()
        if existing is not None:
            return False

        priority = int(round((prop.relevance_score or 0.5) * 100))
        outreach = OutreachQueue(
            property_id=prop.id,
            status="pending",
            priority=max(1, min(100, priority)),
        )
        self.db.add(outreach)
        await self.db.flush()
        return True

    async def _blocklist_contacts(
        self, property_id: UUID, reason: str
    ) -> int:
        """Copy every contact belonging to the property into do_not_contact."""
        stmt = select(PropertyContact).where(PropertyContact.property_id == property_id)
        contacts = list((await self.db.execute(stmt)).scalars().all())

        added = 0
        for contact in contacts:
            if contact.contact_type not in ("phone", "email", "whatsapp"):
                continue
            dnc_stmt = select(DoNotContact).where(
                DoNotContact.contact_type == contact.contact_type,
                DoNotContact.contact_value == contact.normalized_value,
            )
            if (await self.db.execute(dnc_stmt)).scalar_one_or_none() is not None:
                continue
            self.db.add(
                DoNotContact(
                    contact_type=contact.contact_type,
                    contact_value=contact.normalized_value,
                    reason=reason,
                )
            )
            added += 1
        await self.db.flush()
        return added

    # --- Internals ---

    def _apply_candidate_fields(
        self, prop: Property, data: PropertyUpsertFromCandidate
    ) -> None:
        # Update only the fields the candidate is authoritative for.
        if data.canonical_name and prop.canonical_name != data.canonical_name:
            prop.canonical_name = data.canonical_name
            prop.normalized_name = normalize_name(data.canonical_name)
        if data.lat is not None:
            prop.lat = data.lat
        if data.lng is not None:
            prop.lng = data.lng
        if data.lat is not None and data.lng is not None:
            prop.location = _geography_point(data.lat, data.lng)
        if data.locality:
            prop.locality = data.locality
        if data.state:
            prop.state = data.state
        if data.pincode:
            prop.pincode = data.pincode
        if data.google_rating is not None:
            prop.google_rating = data.google_rating
        if data.google_review_count is not None:
            prop.google_review_count = data.google_review_count
        if data.website and not prop.canonical_website:
            prop.canonical_website = data.website
        if data.features_json:
            prop.features_json = {**(prop.features_json or {}), **data.features_json}


# --- Module helpers ---

_NAME_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_NAME_WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    'The Oberoi, Mumbai' -> 'the oberoi mumbai'
    Used for dedup matching by M6. Stored on the row to avoid recomputing.
    """
    cleaned = _NAME_PUNCT_RE.sub(" ", name.lower())
    return _NAME_WS_RE.sub(" ", cleaned).strip()


def _geography_point(lat: float | None, lng: float | None) -> str | None:
    """Build a WKT POINT for the GEOGRAPHY column, or None on missing coords.

    PostGIS accepts the WKT form 'POINT(lng lat)' (note the order).
    On SQLite (test mode), the column is a String so we just store the WKT
    and ignore spatial semantics.
    """
    if lat is None or lng is None:
        return None
    return f"SRID=4326;POINT({lng} {lat})"


def _apply_sort(stmt: Any, sort: str) -> Any:
    match sort:
        case "relevance_score_desc":
            return stmt.order_by(
                Property.relevance_score.desc().nulls_last(),
                Property.created_at.desc(),
            )
        case "relevance_score_asc":
            return stmt.order_by(
                Property.relevance_score.asc().nulls_first(),
                Property.created_at.desc(),
            )
        case "created_at_desc":
            return stmt.order_by(Property.created_at.desc())
        case "created_at_asc":
            return stmt.order_by(Property.created_at.asc())
        case "canonical_name_asc":
            return stmt.order_by(Property.canonical_name.asc())
        case _:
            return stmt.order_by(Property.created_at.desc())


def _assert_transition(current: str, allowed_from: set[str], action: str) -> None:
    """Raise ConflictError if `action` is not valid from the current status."""
    if current not in allowed_from:
        raise ConflictError(
            f"cannot {action} a property with status '{current}' "
            f"(expected one of {sorted(allowed_from)})"
        )
