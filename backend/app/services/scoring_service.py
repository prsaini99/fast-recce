"""ScoringService — M7. Relevance scoring for shoot-location discovery.

Weighted formula (from PRD):
  relevance_score = 0.20*type_fit       + 0.20*shoot_fit
                  + 0.15*visual_uniqueness
                  + 0.10*location_demand + 0.10*contact_completeness
                  + 0.10*website_quality + 0.10*activity_recency
                  + 0.05*ease_of_outreach

Six factors are deterministic; two (shoot_fit, visual_uniqueness) consult
Gemini via LLMClient with a heuristic fallback.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.llm import LLMClient, LLMScoreResult
from app.models.contact import PropertyContact
from app.models.property import Property
from app.schemas.scoring import BatchScoringResult, ScoringResult, SubScore
from app.services.contact_service import ContactService
from app.services.property_service import PropertyService

# Factor weights — changing these requires re-scoring (or monthly recalibration).
_WEIGHTS = {
    "type_fit": 0.20,
    "shoot_fit": 0.20,
    "visual_uniqueness": 0.15,
    "location_demand": 0.10,
    "contact_completeness": 0.10,
    "website_quality": 0.10,
    "activity_recency": 0.10,
    "ease_of_outreach": 0.05,
}

# Per-property-type shoot-affinity. Shoot-heavy types (heritage, warehouse) higher.
_TYPE_FIT: dict[str, float] = {
    "heritage_home": 0.95,
    "theatre_studio": 0.95,
    "villa": 0.90,
    "bungalow": 0.90,
    "resort": 0.90,
    "warehouse": 0.85,
    "farmhouse": 0.85,
    "boutique_hotel": 0.80,
    "banquet_hall": 0.75,
    "industrial_shed": 0.75,
    "rooftop_venue": 0.75,
    "club_lounge": 0.65,
    "coworking_space": 0.55,
    "cafe": 0.60,
    "restaurant": 0.50,
    "office_space": 0.40,
    "school_campus": 0.60,
    "other": 0.30,
}

# Per-city shoot-demand baseline (MVP cities from the PRD + common others).
_LOCATION_DEMAND: dict[str, float] = {
    "Mumbai": 0.95,
    "Alibaug": 0.90,
    "Lonavala": 0.85,
    "Khandala": 0.85,
    "Pune": 0.80,
    "Thane": 0.70,
    "Navi Mumbai": 0.65,
    "Goa": 0.85,
    "Delhi": 0.85,
    "Bangalore": 0.70,
    "Hyderabad": 0.65,
}


class ScoringService:
    def __init__(
        self,
        db: AsyncSession,
        llm_client: LLMClient,
        property_service: PropertyService,
        contact_service: ContactService,
    ) -> None:
        self.db = db
        self.llm = llm_client
        self.property_service = property_service
        self.contact_service = contact_service

    # --- Public ---

    async def score_property(self, property_id: UUID) -> ScoringResult:
        """Score a single property. Writes results to the DB."""
        prop = await self.property_service.get(property_id)
        contacts = await self.contact_service.get_contacts_for_property(property_id)
        features = prop.features_json or {}

        # Pull rich context we've already stashed in features_json so the
        # LLM reasons from editorial summaries + review snippets + the
        # source listing URL rather than just name + type.
        google_ctx = features.get("google_context") or {}
        editorial = (
            google_ctx.get("editorial_summary") if isinstance(google_ctx, dict) else None
        )
        review_snippets = (
            list(google_ctx.get("review_snippets") or [])
            if isinstance(google_ctx, dict)
            else None
        )
        source_url = (
            features.get("external_url")
            or features.get("airbnb_url")
            or prop.canonical_website
        )

        type_fit = SubScore(
            name="type_fit",
            value=_TYPE_FIT.get(prop.property_type, 0.30),
            weight=_WEIGHTS["type_fit"],
            source="deterministic",
            reasoning=f"property_type={prop.property_type}",
        )

        shoot_fit_llm = await self.llm.assess_shoot_fit(
            property_type=prop.property_type,
            description=features.get("description"),
            amenities=list(features.get("amenities") or []),
            feature_tags=list(features.get("feature_tags") or []),
            source_url=source_url if isinstance(source_url, str) else None,
            editorial_summary=editorial if isinstance(editorial, str) else None,
            review_snippets=review_snippets,
        )
        shoot_fit = SubScore(
            name="shoot_fit",
            value=_clamp(shoot_fit_llm.score),
            weight=_WEIGHTS["shoot_fit"],
            source=_score_source_from_llm(shoot_fit_llm),
            reasoning=shoot_fit_llm.reasoning,
        )

        visual_llm = await self.llm.assess_visual_uniqueness(
            property_type=prop.property_type,
            description=features.get("description"),
            amenities=list(features.get("amenities") or []),
            feature_tags=list(features.get("feature_tags") or []),
            source_url=source_url if isinstance(source_url, str) else None,
            editorial_summary=editorial if isinstance(editorial, str) else None,
            review_snippets=review_snippets,
        )
        visual = SubScore(
            name="visual_uniqueness",
            value=_clamp(visual_llm.score),
            weight=_WEIGHTS["visual_uniqueness"],
            source=_score_source_from_llm(visual_llm),
            reasoning=visual_llm.reasoning,
        )

        location_demand = SubScore(
            name="location_demand",
            value=_LOCATION_DEMAND.get(prop.city, 0.50),
            weight=_WEIGHTS["location_demand"],
            source="deterministic",
            reasoning=f"city={prop.city}",
        )

        completeness_val = await self.contact_service.compute_contact_completeness(
            property_id
        )
        contact_completeness = SubScore(
            name="contact_completeness",
            value=completeness_val,
            weight=_WEIGHTS["contact_completeness"],
            source="deterministic",
            reasoning=f"types={sorted({c.contact_type for c in contacts})}",
        )

        website_val, website_reason = _score_website_quality(prop, features)
        website_quality = SubScore(
            name="website_quality",
            value=website_val,
            weight=_WEIGHTS["website_quality"],
            source="deterministic",
            reasoning=website_reason,
        )

        recency_val, recency_reason = _score_activity_recency(prop)
        activity_recency = SubScore(
            name="activity_recency",
            value=recency_val,
            weight=_WEIGHTS["activity_recency"],
            source="deterministic",
            reasoning=recency_reason,
        )

        ease_val, ease_reason = _score_ease_of_outreach(contacts)
        ease_of_outreach = SubScore(
            name="ease_of_outreach",
            value=ease_val,
            weight=_WEIGHTS["ease_of_outreach"],
            source="deterministic",
            reasoning=ease_reason,
        )

        sub_scores = [
            type_fit,
            shoot_fit,
            visual,
            location_demand,
            contact_completeness,
            website_quality,
            activity_recency,
            ease_of_outreach,
        ]
        relevance = sum(s.value * s.weight for s in sub_scores)
        relevance = _clamp(relevance)

        # Persist.
        prop.relevance_score = relevance
        prop.score_reason_json = _build_reason_payload(sub_scores)
        prop.scored_at = datetime.now(UTC)
        await self.db.flush()

        return ScoringResult(
            property_id=property_id,
            relevance_score=relevance,
            sub_scores=sub_scores,
            llm_sub_scores_used_fallback=any(
                s.source == "fallback" for s in (shoot_fit, visual)
            ),
        )

    async def score_batch(
        self,
        *,
        limit: int | None = None,
        only_unscored: bool = True,
        per_property_delay_seconds: float = 0.0,
    ) -> BatchScoringResult:
        """Score every (un)scored property. Returns aggregate stats.

        `per_property_delay_seconds` throttles between properties so we stay
        under provider RPM quotas (Gemini free tier is 5 RPM as of 2026-04
        for gemini-2.5-flash; pass ~25s when running on free tier).
        """
        start = time.monotonic()

        stmt = select(Property).where(Property.is_duplicate.is_(False))
        if only_unscored:
            stmt = stmt.where(Property.scored_at.is_(None))
        stmt = stmt.order_by(Property.created_at.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        properties = list((await self.db.execute(stmt)).scalars().all())

        scored = 0
        failed = 0
        fallbacks = 0
        scores_sum = 0.0
        top_id: UUID | None = None
        top_score = -1.0

        for i, prop in enumerate(properties):
            if i > 0 and per_property_delay_seconds > 0:
                await asyncio.sleep(per_property_delay_seconds)
            try:
                result = await self.score_property(prop.id)
            except Exception as exc:  # noqa: BLE001 — per-property isolation
                failed += 1
                # Log via property_changes in the future — for now, just print.
                print(f"  scoring failed for {prop.id}: {exc}")
                continue

            scored += 1
            if result.llm_sub_scores_used_fallback:
                fallbacks += 1
            scores_sum += result.relevance_score
            if result.relevance_score > top_score:
                top_score = result.relevance_score
                top_id = result.property_id

        return BatchScoringResult(
            scored=scored,
            failed=failed,
            llm_fallbacks_used=fallbacks,
            avg_score=round(scores_sum / scored, 3) if scored else 0.0,
            top_property_id=top_id,
            top_property_score=round(top_score, 3) if top_id else None,
            duration_seconds=round(time.monotonic() - start, 3),
        )


# --- Deterministic sub-score helpers ---


def _score_website_quality(
    prop: Property, features: dict[str, Any]
) -> tuple[float, str]:
    if not prop.canonical_website:
        return 0.0, "no website"
    tags = set(features.get("feature_tags") or [])
    if "events" in tags or "film_friendly" in tags:
        return 0.9, "has website + event/shoot-friendly signals"
    description = features.get("description")
    if description and len(description) > 120:
        return 0.75, "has website with substantive description"
    return 0.5, "has website"


def _score_activity_recency(prop: Property) -> tuple[float, str]:
    reviews = prop.google_review_count or 0
    if reviews >= 100:
        return 0.9, f"{reviews} Google reviews (active)"
    if reviews >= 20:
        return 0.7, f"{reviews} Google reviews"
    if reviews >= 5:
        return 0.5, f"{reviews} Google reviews (low volume)"
    return 0.3, "no/minimal Google reviews"


def _score_ease_of_outreach(contacts: list[PropertyContact]) -> tuple[float, str]:
    type_set = {c.contact_type for c in contacts}
    has_whatsapp = "whatsapp" in type_set
    has_phone = "phone" in type_set
    has_email = "email" in type_set
    has_form = "form" in type_set
    only_instagram = type_set == {"instagram"}

    if has_whatsapp:
        return 0.9, "WhatsApp available"
    if has_phone and has_email:
        return 0.8, "direct phone + email"
    if has_phone:
        return 0.6, "phone only"
    if has_form:
        return 0.5, "contact form only"
    if has_email:
        return 0.5, "email only"
    if only_instagram:
        return 0.2, "Instagram only"
    return 0.0, "no contacts"


# --- Private helpers ---


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _score_source_from_llm(r: LLMScoreResult) -> str:
    # SubScore.source takes "deterministic" | "llm" | "fallback" — map accordingly.
    return "llm" if r.source == "llm" else "fallback"


def _build_reason_payload(sub_scores: list[SubScore]) -> dict[str, Any]:
    return {
        "sub_scores": [s.model_dump() for s in sub_scores],
        "weights": dict(_WEIGHTS),
    }
