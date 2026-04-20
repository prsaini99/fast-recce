"""ContactService — M5. Resolves, validates, and persists property contacts.

Responsibilities:
- Normalize contact values (phones to digits-only, emails lowercased)
- Dedupe across (property_id, contact_type, normalized_value)
- Apply confidence based on extraction method (precedence rules from PRD)
- Flag personal-looking contacts for manual review
- Enforce do-not-contact blocklist before persistence
- Compute contact completeness for the scoring engine (M7)
- Pick canonical phone/email/website and write back to properties
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import NotFoundError
from app.models.contact import DoNotContact, PropertyContact
from app.schemas.contact import ContactResolutionResult, DoNotContactCreate
from app.schemas.crawl import ExtractedContact
from app.services.property_service import PropertyService

# Confidence per extraction method (precedence from PRD, Section 5).
_METHOD_CONFIDENCE: dict[str, float] = {
    "api_structured": 0.95,
    "html_tel_link": 0.90,
    "html_mailto": 0.90,
    "schema_org": 0.85,
    "whatsapp_link": 0.80,
    "meta_tag": 0.65,
    "text_regex": 0.60,
    "contact_form": 0.50,
    "instagram": 0.30,
    "manual": 0.95,
}

_PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "yahoo.co.in",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "rediffmail.com",
        "icloud.com",
        "ymail.com",
        "protonmail.com",
    }
)


class ContactService:
    def __init__(self, db: AsyncSession, property_service: PropertyService):
        self.db = db
        self.property_service = property_service

    # --- Public ---

    async def resolve_contacts(
        self,
        property_id: UUID,
        api_contacts: Iterable[ExtractedContact],
        crawl_contacts: Iterable[ExtractedContact],
    ) -> ContactResolutionResult:
        """Merge API + crawl contacts and persist canonical PropertyContact rows."""
        # Make sure the property exists (raises NotFoundError otherwise).
        await self.property_service.get(property_id)

        all_extracted = list(api_contacts) + list(crawl_contacts)
        contacts_in = len(all_extracted)

        # 1. Normalize + assign canonical confidence.
        normalized: list[_NormalizedContact] = []
        for c in all_extracted:
            n = _normalize(c)
            if n is not None:
                normalized.append(n)

        # 2. Within-batch dedup: keep the highest-confidence per (type, normalized_value).
        deduped: dict[tuple[str, str], _NormalizedContact] = {}
        for n in normalized:
            key = (n.contact_type, n.normalized_value)
            if key not in deduped or deduped[key].confidence < n.confidence:
                deduped[key] = n

        # 3. DNC check (drop blocked contacts entirely).
        dnc_blocked = 0
        survivors: list[_NormalizedContact] = []
        for n in deduped.values():
            if await self.is_blocked(n.contact_type, n.normalized_value):
                dnc_blocked += 1
                continue
            survivors.append(n)

        # 4. Persist (UPSERT pattern).
        flagged = 0
        existing_rows = await self._existing_for_property(property_id)
        existing_keys = {
            (r.contact_type, r.normalized_value): r for r in existing_rows
        }

        for n in survivors:
            key = (n.contact_type, n.normalized_value)
            if key in existing_keys:
                row = existing_keys[key]
                # Update on stronger confidence; always refresh last_seen_at.
                if n.confidence > row.confidence:
                    row.confidence = n.confidence
                    row.contact_value = n.contact_value
                    row.source_name = n.source_name
                    row.source_url = n.source_url
                    row.extraction_method = n.extraction_method
                    row.is_public_business_contact = n.is_public_business_contact
                    row.flagged_personal = n.flagged_personal
                # SQLAlchemy onupdate=func.now() handles last_seen_at.
                if n.flagged_personal:
                    flagged += 1
                continue

            self.db.add(
                PropertyContact(
                    property_id=property_id,
                    contact_type=n.contact_type,
                    contact_value=n.contact_value,
                    normalized_value=n.normalized_value,
                    source_name=n.source_name,
                    source_url=n.source_url,
                    extraction_method=n.extraction_method,
                    confidence=n.confidence,
                    is_public_business_contact=n.is_public_business_contact,
                    flagged_personal=n.flagged_personal,
                )
            )
            if n.flagged_personal:
                flagged += 1

        await self.db.flush()

        # 5. Pick canonical contacts and write back to properties.
        canonical = await self._select_canonical_contacts(property_id)
        return ContactResolutionResult(
            property_id=property_id,
            contacts_in=contacts_in,
            contacts_persisted=len(survivors),
            contacts_blocked_by_dnc=dnc_blocked,
            contacts_flagged_personal=flagged,
            canonical_phone=canonical.phone,
            canonical_email=canonical.email,
            canonical_website=canonical.website,
        )

    async def get_contacts_for_property(
        self, property_id: UUID
    ) -> list[PropertyContact]:
        stmt = (
            select(PropertyContact)
            .where(PropertyContact.property_id == property_id)
            .order_by(PropertyContact.confidence.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def compute_contact_completeness(self, property_id: UUID) -> float:
        """Score 0-1 reflecting how reachable the property is. Used by M7."""
        contacts = await self.get_contacts_for_property(property_id)
        types = {c.contact_type for c in contacts}
        has_phone = "phone" in types
        has_email = "email" in types
        has_website = bool(
            (await self.property_service.get(property_id)).canonical_website
        )

        if has_phone and has_email and has_website:
            return 1.0
        if has_phone and has_email:
            return 0.8
        if has_phone:
            return 0.5
        if has_email:
            return 0.4
        if "form" in types or "whatsapp" in types:
            return 0.3
        return 0.0

    # --- Do-not-contact ---

    async def is_blocked(self, contact_type: str, normalized_value: str) -> bool:
        # Check direct match.
        stmt = select(DoNotContact).where(
            DoNotContact.contact_type == contact_type,
            DoNotContact.contact_value == normalized_value,
        )
        if (await self.db.execute(stmt)).scalar_one_or_none() is not None:
            return True

        # Domain-level email blocklist: 'foo@blocked.com' blocked if 'blocked.com' is on DNC.
        if contact_type == "email" and "@" in normalized_value:
            domain = normalized_value.split("@", 1)[1]
            stmt = select(DoNotContact).where(
                DoNotContact.contact_type == "domain",
                DoNotContact.contact_value == domain,
            )
            if (await self.db.execute(stmt)).scalar_one_or_none() is not None:
                return True

        return False

    async def add_to_do_not_contact(
        self,
        data: DoNotContactCreate,
    ) -> DoNotContact:
        normalized = _normalize_dnc_value(data.contact_type, data.contact_value)
        existing = await self.db.execute(
            select(DoNotContact).where(
                DoNotContact.contact_type == data.contact_type,
                DoNotContact.contact_value == normalized,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return row
        row = DoNotContact(
            contact_type=data.contact_type,
            contact_value=normalized,
            reason=data.reason,
        )
        self.db.add(row)
        await self.db.flush()
        await self.db.refresh(row)
        return row

    # --- Internals ---

    async def _existing_for_property(
        self, property_id: UUID
    ) -> list[PropertyContact]:
        stmt = select(PropertyContact).where(
            PropertyContact.property_id == property_id
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _select_canonical_contacts(
        self, property_id: UUID
    ) -> "_CanonicalContacts":
        contacts = await self.get_contacts_for_property(property_id)

        # Reset is_primary, then re-elect.
        for c in contacts:
            c.is_primary = False

        phone = _pick_best(contacts, "phone")
        email = _pick_best(contacts, "email")
        website_contact = _pick_best(contacts, "website")
        for picked in (phone, email, website_contact):
            if picked is not None:
                picked.is_primary = True

        await self.db.flush()

        prop = await self.property_service.get(property_id)
        # Website preference: existing canonical_website (set from the
        # discovery candidate), else a contacts row of type 'website'.
        website_value = prop.canonical_website or (
            website_contact.contact_value if website_contact else None
        )
        await self.property_service.update_canonical_contacts(
            property_id,
            phone=phone.contact_value if phone else None,
            email=email.contact_value if email else None,
            website=website_value,
        )

        return _CanonicalContacts(
            phone=phone.contact_value if phone else None,
            email=email.contact_value if email else None,
            website=website_value,
        )


# --- Module-private types & helpers ---


class _NormalizedContact:
    __slots__ = (
        "contact_type",
        "contact_value",
        "normalized_value",
        "source_name",
        "source_url",
        "extraction_method",
        "confidence",
        "is_public_business_contact",
        "flagged_personal",
    )

    def __init__(
        self,
        contact_type: str,
        contact_value: str,
        normalized_value: str,
        source_name: str,
        source_url: str | None,
        extraction_method: str,
        confidence: float,
        is_public_business_contact: bool,
        flagged_personal: bool,
    ) -> None:
        self.contact_type = contact_type
        self.contact_value = contact_value
        self.normalized_value = normalized_value
        self.source_name = source_name
        self.source_url = source_url
        self.extraction_method = extraction_method
        self.confidence = confidence
        self.is_public_business_contact = is_public_business_contact
        self.flagged_personal = flagged_personal


class _CanonicalContacts:
    __slots__ = ("phone", "email", "website")

    def __init__(
        self, phone: str | None, email: str | None, website: str | None
    ) -> None:
        self.phone = phone
        self.email = email
        self.website = website


def _normalize(c: ExtractedContact) -> _NormalizedContact | None:
    """Normalize a single ExtractedContact. Returns None if unusable."""
    confidence = _METHOD_CONFIDENCE.get(c.extraction_method, 0.5)

    if c.contact_type == "phone":
        normalized = normalize_phone(c.value)
        if normalized is None:
            return None
        flagged = False
        is_business = confidence >= 0.85
        return _NormalizedContact(
            contact_type="phone",
            contact_value=c.value.strip(),
            normalized_value=normalized,
            source_name=_infer_source_name(c),
            source_url=c.source_url,
            extraction_method=c.extraction_method,
            confidence=confidence,
            is_public_business_contact=is_business,
            flagged_personal=flagged,
        )

    if c.contact_type == "email":
        normalized = normalize_email(c.value)
        if normalized is None:
            return None
        domain = normalized.split("@", 1)[1]
        is_personal = domain in _PERSONAL_EMAIL_DOMAINS
        is_business = (not is_personal) and confidence >= 0.85
        return _NormalizedContact(
            contact_type="email",
            contact_value=c.value.strip(),
            normalized_value=normalized,
            source_name=_infer_source_name(c),
            source_url=c.source_url,
            extraction_method=c.extraction_method,
            confidence=confidence,
            is_public_business_contact=is_business,
            flagged_personal=is_personal,
        )

    if c.contact_type == "whatsapp":
        digits = _digits_from_url(c.value)
        if not digits:
            return None
        return _NormalizedContact(
            contact_type="whatsapp",
            contact_value=c.value.strip(),
            normalized_value=digits,
            source_name=_infer_source_name(c),
            source_url=c.source_url,
            extraction_method=c.extraction_method,
            confidence=confidence,
            is_public_business_contact=True,
            flagged_personal=False,
        )

    if c.contact_type in ("form", "website", "instagram"):
        url = c.value.strip()
        if not url:
            return None
        normalized_url = url.lower().rstrip("/")
        return _NormalizedContact(
            contact_type=c.contact_type,
            contact_value=url,
            normalized_value=normalized_url,
            source_name=_infer_source_name(c),
            source_url=c.source_url,
            extraction_method=c.extraction_method,
            confidence=confidence,
            is_public_business_contact=c.contact_type != "instagram",
            flagged_personal=False,
        )

    return None


_PHONE_DIGITS_RE = re.compile(r"\d+")


def normalize_phone(raw: str) -> str | None:
    """Strip all non-digits. Indian numbers normalized to '91XXXXXXXXXX'.

    Returns None if the result has fewer than 10 digits or more than 15.
    """
    digits = "".join(_PHONE_DIGITS_RE.findall(raw))
    if not (10 <= len(digits) <= 15):
        return None
    # Indian mobile number without country code → prepend 91.
    if len(digits) == 10 and digits[0] in "6789":
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
        digits = "91" + digits[1:]
    return digits


_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


def normalize_email(raw: str) -> str | None:
    cleaned = raw.strip().lower()
    if not _EMAIL_RE.match(cleaned):
        return None
    return cleaned


def _digits_from_url(url: str) -> str | None:
    digits = "".join(c for c in url if c.isdigit())
    if 10 <= len(digits) <= 15:
        if len(digits) == 10 and digits[0] in "6789":
            digits = "91" + digits
        return digits
    return None


def _infer_source_name(c: ExtractedContact) -> str:
    if c.extraction_method == "api_structured":
        return "google_places"
    if c.source_url:
        host = urlparse(c.source_url).netloc.lower()
        return host or "property_website"
    return "property_website"


def _normalize_dnc_value(contact_type: str, value: str) -> str:
    if contact_type in ("phone", "whatsapp"):
        return normalize_phone(value) or value
    if contact_type == "email":
        return (normalize_email(value) or value).lower()
    if contact_type == "domain":
        return value.lower().lstrip(".").rstrip("/")
    return value


def _pick_best(
    contacts: list[PropertyContact], contact_type: str
) -> PropertyContact | None:
    candidates = [c for c in contacts if c.contact_type == contact_type]
    if not candidates:
        return None
    # Prefer business + verified + highest confidence.
    candidates.sort(
        key=lambda c: (
            c.is_public_business_contact,
            not c.flagged_personal,
            c.confidence,
        ),
        reverse=True,
    )
    return candidates[0]
