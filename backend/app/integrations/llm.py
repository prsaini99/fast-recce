"""Gemini LLM client for subjective scoring (M7) and brief generation (M8).

Uses the modern `google-genai` SDK. Two subjective signals need the LLM:
- shoot_fit: how suitable is this property for a film/ad shoot?
- visual_uniqueness: how visually distinctive is the property?

Both are returned as floats in 0-1 via Gemini's JSON mode with response_schema.
Every public method has a deterministic fallback so a Gemini outage never
stops the pipeline — we fall back to keyword heuristics and flag the
property so it gets re-scored when the API is back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class _ScoreJSON(BaseModel):
    """Structured output schema the LLM must return."""

    score: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=200)


@dataclass
class LLMScoreResult:
    """Result returned to ScoringService.

    source is one of:
      - "llm"        : Gemini returned a valid score
      - "fallback"   : LLM failed, heuristic used
    """

    score: float
    reasoning: str
    source: str


@dataclass
class LLMTextResult:
    """Result returned for free-form text generation (briefs, outreach angles)."""

    text: str
    source: str  # "llm" | "fallback"


def _format_listing_specifics(
    *,
    price_display: str | None,
    bedrooms: int | None,
    bathrooms: int | None,
    area_sqft: float | None,
    max_guests: int | None,
    property_subtype: str | None,
) -> str:
    """Compact single-line summary of scraped listing specifics.

    The LLM receives something like:
      "2 BHK · 1 bath · 980 sqft · ₹1.15 Cr · Apartment"
    Only non-null fields are included so the prompt stays terse.
    """
    parts: list[str] = []
    if bedrooms is not None and bedrooms > 0:
        parts.append(f"{bedrooms} BHK")
    if bathrooms is not None and bathrooms > 0:
        parts.append(f"{bathrooms} bath" + ("s" if bathrooms != 1 else ""))
    if area_sqft is not None and area_sqft > 0:
        parts.append(f"{int(area_sqft)} sqft")
    if max_guests is not None and max_guests > 0:
        parts.append(f"fits {max_guests} guests")
    if price_display:
        parts.append(price_display)
    if property_subtype:
        parts.append(property_subtype)
    return " · ".join(parts)


# Keywords that strongly suggest shoot-readiness (used in fallback heuristic).
_SHOOT_FIT_KEYWORDS = {
    "photoshoot", "photo shoot", "film shoot", "shoot-ready", "film friendly",
    "events", "wedding", "reception", "brand campaign", "campaign",
    "rooftop", "lawn", "terrace", "garden", "poolside",
    "industrial", "heritage", "rustic", "minimalist", "open-air",
}


class LLMClient:
    """Async Gemini client. Stateless — safe to construct once and reuse."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3-flash-preview",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._timeout = timeout_seconds

    async def assess_shoot_fit(
        self,
        *,
        property_type: str,
        description: str | None,
        amenities: list[str],
        feature_tags: list[str],
        source_url: str | None = None,
        editorial_summary: str | None = None,
        review_snippets: list[str] | None = None,
    ) -> LLMScoreResult:
        """How well does this property fit a shoot use-case? Returns 0-1."""
        lines = [
            f"Property type: {property_type}",
            f"Description: {description or '(no description available)'}",
            f"Amenities: {', '.join(amenities) if amenities else '(none)'}",
            f"Feature tags: {', '.join(feature_tags) if feature_tags else '(none)'}",
        ]
        if editorial_summary:
            lines.append(f"Editorial summary: {editorial_summary}")
        if review_snippets:
            lines.append("Review snippets: " + " | ".join(s[:250] for s in review_snippets[:3]))
        if source_url:
            lines.append(f"Source URL: {source_url}")

        prompt = (
            "\n".join(lines)
            + "\n\n"
            + "Assess how suitable this property is for hosting a film, ad, "
              "or photoshoot on a scale of 0.0 to 1.0. Consider:\n"
              "- Does it have varied shooting spaces (indoor/outdoor)?\n"
              "- Are there signals of event/shoot hosting?\n"
              "- Is the aesthetic distinctive (not generic)?\n"
              "- Does it have practical shoot amenities (parking, power, wifi)?\n"
              "\n"
              "Return a JSON object with `score` (0-1) and `reasoning` (one sentence)."
        )
        return await self._ask_for_score(
            prompt=prompt,
            fallback=self._shoot_fit_heuristic,
            fallback_args=(description, amenities, feature_tags),
        )

    async def assess_visual_uniqueness(
        self,
        *,
        property_type: str,
        description: str | None,
        amenities: list[str],
        feature_tags: list[str],
        source_url: str | None = None,
        editorial_summary: str | None = None,
        review_snippets: list[str] | None = None,
    ) -> LLMScoreResult:
        """How visually distinctive is this property? Returns 0-1."""
        lines = [
            f"Property type: {property_type}",
            f"Description: {description or '(no description available)'}",
            f"Amenities: {', '.join(amenities) if amenities else '(none)'}",
            f"Feature tags: {', '.join(feature_tags) if feature_tags else '(none)'}",
        ]
        if editorial_summary:
            lines.append(f"Editorial summary: {editorial_summary}")
        if review_snippets:
            lines.append("Review snippets: " + " | ".join(s[:250] for s in review_snippets[:3]))
        if source_url:
            lines.append(f"Source URL: {source_url}")
        prompt = (
            "\n".join(lines)
            + "\n\n"
            + "Assess how visually unique this property looks on a scale of 0.0 to 1.0.\n"
            "- 0.1: generic apartment / cookie-cutter hotel room\n"
            "- 0.5: pleasant but not distinctive\n"
            "- 0.9: strong visual identity (heritage architecture, striking views, "
            "unusual interiors)\n"
            "\n"
            "Return a JSON object with `score` (0-1) and `reasoning` (one sentence)."
        )
        return await self._ask_for_score(
            prompt=prompt,
            fallback=self._visual_uniqueness_heuristic,
            fallback_args=(property_type, feature_tags),
        )

    async def generate_brief(
        self,
        *,
        property_name: str,
        city: str,
        locality: str | None,
        property_type: str,
        description: str | None,
        amenities: list[str],
        feature_tags: list[str],
        top_score_factors: list[str],
        contact_summary: str,
        source_url: str | None = None,
        google_rating: float | None = None,
        google_review_count: int | None = None,
        editorial_summary: str | None = None,
        review_snippets: list[str] | None = None,
        price_level: str | None = None,
        price_display: str | None = None,
        bedrooms: int | None = None,
        bathrooms: int | None = None,
        area_sqft: float | None = None,
        max_guests: int | None = None,
        property_subtype: str | None = None,
    ) -> LLMTextResult:
        """Generate a 2-3 sentence operational brief for reviewers.

        Tone: operational (not marketing). Reviewers need to decide fast:
        is this property a good shoot fit, is it reachable, what stands out.

        Optional keyword arguments let callers feed richer context — Google
        Places editorial summaries + review snippets, external-source URL,
        price level — so the LLM isn't reasoning from just name + type.
        """
        context_lines = [
            f"Property: {property_name}",
            f"Type: {property_type}",
            f"Location: {locality + ', ' if locality else ''}{city}",
            f"Description: {description or '(no description available)'}",
            f"Amenities: {', '.join(amenities) if amenities else '(none extracted)'}",
            f"Feature tags: {', '.join(feature_tags) if feature_tags else '(none)'}",
            f"Top scoring factors: {', '.join(top_score_factors) if top_score_factors else '(n/a)'}",
            f"Contactability: {contact_summary}",
        ]
        # Listing specifics (scraped from external sources). Each is only
        # added when populated so we don't pad the prompt with "None".
        specifics = _format_listing_specifics(
            price_display=price_display,
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            area_sqft=area_sqft,
            max_guests=max_guests,
            property_subtype=property_subtype,
        )
        if specifics:
            context_lines.append(f"Listing specifics: {specifics}")
        if google_rating is not None:
            rc = f" ({google_review_count} reviews)" if google_review_count else ""
            context_lines.append(f"Google rating: {google_rating}/5{rc}")
        if editorial_summary:
            context_lines.append(f"Google editorial summary: {editorial_summary}")
        if price_level:
            context_lines.append(f"Price level (Google): {price_level}")
        if review_snippets:
            joined = " | ".join(s[:300] for s in review_snippets[:3])
            context_lines.append(f"Recent review snippets: {joined}")
        if source_url:
            context_lines.append(f"Source URL: {source_url}")

        prompt = (
            "\n".join(context_lines)
            + "\n\n"
            + "Write a 2-3 sentence operational brief for a FastRecce reviewer who\n"
              "is deciding whether to approve this property for shoot-location outreach.\n"
              "Tone: operational, factual, no marketing fluff. Structure:\n"
              "  1. What the property is (type + location + 1-2 distinctive features)\n"
              "  2. Why it's (or isn't) a fit for shoots\n"
              "  3. How reachable it is\n"
              "Return only the brief text — no headings, no bullet points."
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=1000,
                    thinking_config=types.ThinkingConfig(thinking_budget=256),
                ),
            )
        except genai_errors.ClientError as exc:
            logger.warning("Gemini ClientError generating brief, using fallback: %s", exc)
            return LLMTextResult(
                text=self._brief_fallback(
                    property_name, property_type, city, amenities, contact_summary
                ),
                source="fallback",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini unavailable for brief, using fallback: %s", exc)
            return LLMTextResult(
                text=self._brief_fallback(
                    property_name, property_type, city, amenities, contact_summary
                ),
                source="fallback",
            )

        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            return LLMTextResult(text=text.strip(), source="llm")

        return LLMTextResult(
            text=self._brief_fallback(
                property_name, property_type, city, amenities, contact_summary
            ),
            source="fallback",
        )

    @staticmethod
    def _brief_fallback(
        property_name: str,
        property_type: str,
        city: str,
        amenities: list[str],
        contact_summary: str,
    ) -> str:
        """Template-based brief when LLM is unavailable. Stays operational."""
        pretty_type = property_type.replace("_", " ")
        amenity_clause = (
            f" with {', '.join(amenities[:4])}"
            if amenities
            else ""
        )
        return (
            f"{property_name} is a {pretty_type} in {city}{amenity_clause}. "
            f"Shoot fit to be evaluated on site visit. "
            f"Contactability: {contact_summary}."
        )

    # --- Internals ---

    async def _ask_for_score(
        self,
        *,
        prompt: str,
        fallback: object,  # callable -> tuple[float, str]
        fallback_args: tuple,
    ) -> LLMScoreResult:
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_ScoreJSON,
                    temperature=0.2,
                    # Bigger budget — gemini-2.5+ models use part of the
                    # output_tokens for hidden "thinking" before producing
                    # the visible JSON. 1000 leaves comfortable room for both.
                    max_output_tokens=1000,
                    # Cap the hidden reasoning budget for predictability/cost.
                    thinking_config=types.ThinkingConfig(thinking_budget=256),
                ),
            )
        except genai_errors.ClientError as exc:
            # 4xx — bad request, bad key, quota. Don't retry, fall back.
            logger.warning("Gemini ClientError, using fallback: %s", exc)
            score, reasoning = fallback(*fallback_args)  # type: ignore[operator]
            return LLMScoreResult(score=score, reasoning=reasoning, source="fallback")
        except (genai_errors.ServerError, Exception) as exc:  # noqa: BLE001
            logger.warning("Gemini unavailable, using fallback: %s", exc)
            score, reasoning = fallback(*fallback_args)  # type: ignore[operator]
            return LLMScoreResult(score=score, reasoning=reasoning, source="fallback")

        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, _ScoreJSON):
            return LLMScoreResult(
                score=float(parsed.score),
                reasoning=parsed.reasoning,
                source="llm",
            )

        # Parse manually if parsed is missing (older SDK behaviour).
        text = getattr(response, "text", None)
        if isinstance(text, str):
            try:
                parsed_manual = _ScoreJSON.model_validate_json(text)
                return LLMScoreResult(
                    score=float(parsed_manual.score),
                    reasoning=parsed_manual.reasoning,
                    source="llm",
                )
            except Exception:  # noqa: BLE001
                pass

        logger.warning("Gemini response unparseable; using fallback")
        score, reasoning = fallback(*fallback_args)  # type: ignore[operator]
        return LLMScoreResult(score=score, reasoning=reasoning, source="fallback")

    @staticmethod
    def _shoot_fit_heuristic(
        description: str | None,
        amenities: list[str],
        feature_tags: list[str],
    ) -> tuple[float, str]:
        corpus = " ".join(
            [
                (description or "").lower(),
                " ".join(amenities).lower(),
                " ".join(feature_tags).lower(),
            ]
        )
        hits = sum(1 for kw in _SHOOT_FIT_KEYWORDS if kw in corpus)
        # 0 hits -> 0.3, 1-2 hits -> 0.5, 3-5 hits -> 0.7, 6+ hits -> 0.85
        if hits >= 6:
            score = 0.85
        elif hits >= 3:
            score = 0.7
        elif hits >= 1:
            score = 0.5
        else:
            score = 0.3
        return score, f"heuristic: matched {hits} shoot-fit keyword(s)"

    @staticmethod
    def _visual_uniqueness_heuristic(
        property_type: str,
        feature_tags: list[str],
    ) -> tuple[float, str]:
        # Heritage / rustic / industrial signal strong uniqueness.
        distinctive_tags = {"heritage", "rustic", "industrial", "traditional"}
        matches = [t for t in feature_tags if t in distinctive_tags]

        base = {
            "heritage_home": 0.75,
            "villa": 0.60,
            "farmhouse": 0.60,
            "warehouse": 0.65,
            "theatre_studio": 0.70,
            "bungalow": 0.55,
            "resort": 0.50,
            "boutique_hotel": 0.50,
            "cafe": 0.40,
            "restaurant": 0.35,
            "office_space": 0.30,
        }.get(property_type, 0.50)

        bonus = 0.1 * min(len(matches), 2)
        score = min(1.0, base + bonus)
        return score, f"heuristic: type={property_type}, distinctive_tags={matches}"

    async def close(self) -> None:
        """Release any underlying HTTP resources."""
        close = getattr(self._client, "close", None)
        if callable(close):
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:  # noqa: BLE001
                pass
