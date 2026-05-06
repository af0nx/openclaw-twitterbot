"""Attention gate for enterprise SkinBetHub main-feed posts.

The normal quality gate blocks bad copy. This gate blocks weak copy: posts that
are technically valid but do not give a user a concrete reason to stop, click,
save, or come back for the next update.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from processing.tweet_quality import main_feed_quality_issue, normalize_generated_text, tweet_quality_issue


MAIN_FEED_ATTENTION_PILLARS = {1, 2, 3, 4, 5, 7, 10, 13, 14, 15, 17}
TRUSTED_ATTENTION_SOURCES = {
    "hltv",
    "dust2us",
    "valve_cs2",
    "gocore",
    "gosugamers",
    "prediction_webhook",
    "prediction_results",
}

CONCRETE_EVENT_RE = re.compile(
    r"\b(?:announce|announced|release|released|bench|benched|sign|signed|replace|replaced|"
    r"beat|defeat|defeated|win|won|sweep|swept|qualif|rank|ranking|update|final|"
    r"schedule|format|teams|prize|roster|lineup|vrs)\b",
    re.IGNORECASE,
)
CONSEQUENCE_RE = re.compile(
    r"\b(?:affect|change|changes|shift|shifts|move|moves|matter|matters|impact|stability|depth|balance|"
    r"strength|path|paths|conditions|context|confirmed|review|read|harder|create|creates|"
    r"tracking|re-check|recheck|repeat|repeats|reset|resets|reshapes|decides|needs)\b",
    re.IGNORECASE,
)
RETURN_HOOK_RE = re.compile(
    r"\b(?:next|watch|monitor|tracking|track|re-check|recheck|check|checking|review|before|upcoming|future|slate|matchup|"
    r"bracket|path|veto|lineup|official server|event slate)\b",
    re.IGNORECASE,
)
GENERIC_COPY_RE = re.compile(
    r"\b(?:this is big|huge if true|thoughts|who you got|what do we think|just saying|"
    r"public still sleeping|market has not priced|books still price)\b",
    re.IGNORECASE,
)


def _sentence_count(text: str) -> int:
    return len([part for part in re.split(r"[.!?]+", text) if part.strip()])


def _is_generated_media(preview_path: Optional[str]) -> bool:
    if not preview_path:
        return False
    path = Path(str(preview_path))
    if path.name.startswith("hl_") and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return True
    return "generated_images" in path.parts


def _metadata_has_structure(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    structured_keys = {
        "team1",
        "team2",
        "team_a",
        "team_b",
        "teams",
        "pick",
        "probability",
        "model_probability",
        "confidence",
        "published_at",
    }
    return any(metadata.get(key) for key in structured_keys)


def evaluate_main_feed_attention(
    text: Optional[str],
    *,
    event: Optional[dict[str, Any]] = None,
    pillar: Optional[int] = None,
    media_ref: Optional[str] = None,
    media_preview_path: Optional[str] = None,
    reply_target_id: Optional[str] = None,
    quote_tweet_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return a pass/fail attention score for standalone main-feed posts."""
    event = event or {}
    metadata = event.get("metadata") or {}
    cleaned = normalize_generated_text(text)

    try:
        pillar_int = int(pillar or 0)
    except (TypeError, ValueError):
        pillar_int = 0

    if reply_target_id or quote_tweet_id or (pillar_int and pillar_int not in MAIN_FEED_ATTENTION_PILLARS):
        return {
            "passed": True,
            "score": 100,
            "grade": "SKIP",
            "blockers": [],
            "signals": ["not a standalone main-feed attention surface"],
        }

    blockers: list[str] = []
    signals: list[str] = []
    score = 0

    media_is_photo = bool(media_ref) and not _is_generated_media(media_preview_path)
    if media_is_photo:
        score += 20
        signals.append("real photo/media attached")
    else:
        blockers.append("missing real photo")

    source = str(event.get("source") or "").lower()
    source_url = event.get("source_url")
    category = str(event.get("category") or "")
    source_grounded = (
        source in TRUSTED_ATTENTION_SOURCES
        or bool(source_url)
        or category in {"match_prediction", "match_preview", "match_result"}
        or _metadata_has_structure(metadata)
    )
    if source_grounded:
        score += 20
        signals.append("source or structured event grounding")
    else:
        blockers.append("not grounded in a source or structured event")

    headline = normalize_generated_text(event.get("headline"))
    concrete_event = bool(CONCRETE_EVENT_RE.search(" ".join([headline, cleaned])))
    if concrete_event:
        score += 15
        signals.append("concrete event named")
    else:
        blockers.append("no concrete event")

    if CONSEQUENCE_RE.search(cleaned):
        score += 20
        signals.append("clear consequence stated")
    else:
        blockers.append("no clear consequence")

    if RETURN_HOOK_RE.search(cleaned):
        score += 15
        signals.append("return hook present")
    else:
        blockers.append("no reason to check the next update")

    professional_issue = tweet_quality_issue(cleaned) or main_feed_quality_issue(
        cleaned,
        pillar=pillar_int,
        reply_target_id=reply_target_id,
        quote_tweet_id=quote_tweet_id,
    )
    if professional_issue:
        blockers.append(professional_issue)
    elif _sentence_count(cleaned) >= 2 and not GENERIC_COPY_RE.search(cleaned):
        score += 10
        signals.append("professional multi-sentence copy")
    else:
        blockers.append("copy is too generic or too thin")

    if score >= 90:
        grade = "A"
    elif score >= 80:
        grade = "B"
    elif score >= 70:
        grade = "C"
    else:
        grade = "D"

    return {
        "passed": score >= 85 and not blockers,
        "score": score,
        "grade": grade,
        "blockers": sorted(set(blockers)),
        "signals": sorted(set(signals)),
    }
