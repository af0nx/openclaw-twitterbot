#!/usr/bin/env python3
"""Read-only shadow report for the Twitter generation pipeline.

This does not post, update rows, or upload media. It inspects what the bot has
recently generated and applies the same preflight policies that protect the
main account.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(1, str(REPO_ROOT))

from processing.content_generator import (  # noqa: E402
    is_invalid_tweet_candidate,
    main_feed_quality_issue,
    normalize_generated_text,
)
from processing.attention_quality import evaluate_main_feed_attention  # noqa: E402


MAIN_FEED_TEXT_ONLY_EXEMPT_PILLARS = {12, 16}
POSTLIKE_STATUSES = ("queued", "hitl_pending", "draft", "rejected", "posted")
BETTING_LANGUAGE_REQUIRING_EVIDENCE = re.compile(
    r"\b(?:"
    r"books?|bookmakers?|book price|fair price|price(?:d|s)?|pricing|"
    r"odds?|edge|value|undervalued|overpriced|mispriced|market|markets|"
    r"line|lines|lean|low risk|high risk"
    r")\b",
    re.IGNORECASE,
)
BETTING_EVIDENCE_PATTERN = re.compile(
    r"(?:"
    r"\b\d+(?:\.\d+)?\s*%|"
    r"\b[1-9]\.\d{2}\b|"
    r"\b\d+(?:\.\d+)?\s*(?:edge|odds|price|line)\b"
    r")",
    re.IGNORECASE,
)


def load_runtime_env() -> None:
    load_dotenv(REPO_ROOT / "config" / ".env.production", override=False)
    load_dotenv("/dev/shm/.env", override=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show generated Twitter content without posting anything."
    )
    parser.add_argument("--hours", type=int, default=36, help="Lookback window.")
    parser.add_argument("--limit", type=int, default=25, help="Max rows to show.")
    parser.add_argument(
        "--status",
        action="append",
        choices=POSTLIKE_STATUSES,
        help="Filter by status. Can be repeated. Defaults to common generated states.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser.parse_args()


def connect_db():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(database_url)


def fetch_shadow_rows(conn, *, hours: int, limit: int, statuses: Iterable[str]) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                t.id::text,
                t.created_at,
                t.posted_at,
                t.status,
                COALESCE(t.account_bucket, 'main') AS account_bucket,
                t.pillar,
                t.pillar_name,
                t.content,
                t.reply_target_id,
                t.quote_tweet_id,
                t.media_path,
                t.media_preview_path,
                t.posting_error,
                t.scheduled_post_at,
                t.twitter_tweet_id,
                t.is_thread,
                e.category,
                e.source,
                e.source_url,
                e.headline,
                e.metadata AS event_metadata,
                e.status AS event_status
            FROM twitter_bot.tweets_v2 t
            LEFT JOIN twitter_bot.events e ON e.id = t.event_id
            WHERE t.created_at > NOW() - (%s * INTERVAL '1 hour')
              AND t.status = ANY(%s)
            ORDER BY t.created_at DESC
            LIMIT %s
            """,
            (hours, list(statuses), limit),
        )
        return [dict(row) for row in cur.fetchall()]


def fetch_summary(conn, *, hours: int) -> dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'queued') AS queued,
                COUNT(*) FILTER (WHERE status = 'hitl_pending') AS hitl_pending,
                COUNT(*) FILTER (WHERE status = 'draft') AS draft,
                COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
                COUNT(*) FILTER (WHERE status = 'posted') AS posted,
                COUNT(*) FILTER (WHERE COALESCE(NULLIF(media_path, ''), '') <> '') AS with_media,
                COUNT(*) AS total
            FROM twitter_bot.tweets_v2
            WHERE created_at > NOW() - (%s * INTERVAL '1 hour')
            """,
            (hours,),
        )
        row = dict(cur.fetchone() or {})

        cur.execute("SELECT MAX(posted_at) AS last_posted_at FROM twitter_bot.tweets_v2")
        last_posted_at = (cur.fetchone() or {}).get("last_posted_at")
        row["last_posted_at"] = last_posted_at
        return row


def age_label(value: datetime | None) -> str:
    if not value:
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - value).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def media_state(row: dict[str, Any]) -> dict[str, Any]:
    media = (row.get("media_path") or "").strip()
    preview = (row.get("media_preview_path") or "").strip()
    result = {
        "has_media": bool(media),
        "media_kind": "missing",
        "media_ref": media or None,
        "preview": preview or None,
        "preview_exists": None,
    }
    if media:
        parsed = urlparse(media)
        if parsed.scheme in ("http", "https"):
            result["media_kind"] = "url"
        elif media.isdigit():
            result["media_kind"] = "x_media_id"
        else:
            path = Path(media)
            if not path.is_absolute():
                path = REPO_ROOT / media
            result["media_kind"] = "local_file" if path.exists() else "missing_local_file"
            result["media_resolved_path"] = str(path)

    if preview:
        preview_path = Path(preview)
        if not preview_path.is_absolute():
            preview_path = REPO_ROOT / preview
        result["preview_exists"] = preview_path.exists()
        result["preview_resolved_path"] = str(preview_path)
    return result


def requires_main_feed_media(row: dict[str, Any]) -> bool:
    if row.get("reply_target_id") or row.get("quote_tweet_id"):
        return False
    try:
        pillar = int(row.get("pillar") or 0)
    except (TypeError, ValueError):
        pillar = 0
    return pillar not in MAIN_FEED_TEXT_ONLY_EXEMPT_PILLARS


def is_non_photo_media(media: dict[str, Any]) -> bool:
    preview = media.get("preview")
    if not preview:
        return False
    path = Path(str(preview))
    if path.name.startswith("hl_") and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return True
    return "generated_images" in path.parts


def reply_engagement_enabled() -> bool:
    return os.getenv("ENABLE_REPLY_ENGAGEMENT", "false").lower() in {"1", "true", "yes", "on"}


def evaluate_row(row: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    text = normalize_generated_text(row.get("content"))
    media = media_state(row)
    is_reply_or_quote = bool(row.get("reply_target_id") or row.get("quote_tweet_id"))

    if is_reply_or_quote and not reply_engagement_enabled():
        reasons.append("reply/quote generation disabled")

    if requires_main_feed_media(row) and not media["has_media"]:
        reasons.append("main feed tweet missing media")
    if requires_main_feed_media(row) and is_non_photo_media(media):
        reasons.append("main feed media is generated graphics, not a photo")

    try:
        quality_reason = main_feed_quality_issue(text, pillar=row.get("pillar"))
    except Exception as exc:
        quality_reason = f"quality helper failed: {exc.__class__.__name__}"
    if quality_reason:
        reasons.append(quality_reason)

    try:
        invalid = is_invalid_tweet_candidate(text, pillar=row.get("pillar"))
    except TypeError:
        invalid = is_invalid_tweet_candidate(text)
    if invalid and not quality_reason:
        reasons.append("invalid generated copy")

    if BETTING_LANGUAGE_REQUIRING_EVIDENCE.search(text) and not BETTING_EVIDENCE_PATTERN.search(text):
        reasons.append("betting language without odds/price/probability evidence")

    if media["media_kind"] == "missing_local_file":
        reasons.append("media path points to missing local file")

    attention_result = evaluate_main_feed_attention(
        text,
        event={
            "source": row.get("source"),
            "source_url": row.get("source_url"),
            "category": row.get("category"),
            "headline": row.get("headline"),
            "metadata": row.get("event_metadata") or {},
        },
        pillar=row.get("pillar"),
        media_ref=row.get("media_path"),
        media_preview_path=row.get("media_preview_path"),
        reply_target_id=row.get("reply_target_id"),
        quote_tweet_id=row.get("quote_tweet_id"),
    )
    if not attention_result["passed"]:
        reasons.append(
            f"attention gate failed: score={attention_result['score']} "
            f"blockers={', '.join(attention_result['blockers'])}"
        )

    status = str(row.get("status") or "")
    verdict = "PASS"
    if reasons:
        verdict = "BLOCK"
    elif status == "hitl_pending":
        verdict = "REVIEW"
    elif status == "rejected":
        verdict = "INFO"

    return {
        "id": row.get("id"),
        "status": status,
        "verdict": verdict,
        "reasons": sorted(set(reasons)),
        "created_at": row.get("created_at"),
        "age": age_label(row.get("created_at")),
        "account_bucket": row.get("account_bucket"),
        "pillar": row.get("pillar"),
        "pillar_name": row.get("pillar_name"),
        "category": row.get("category"),
        "source": row.get("source"),
        "source_url": row.get("source_url"),
        "headline": row.get("headline"),
        "attention": attention_result,
        "scheduled_post_at": row.get("scheduled_post_at"),
        "posting_error": row.get("posting_error"),
        "twitter_tweet_id": row.get("twitter_tweet_id"),
        "is_reply_or_quote": is_reply_or_quote,
        "media": media,
        "content": text,
    }


def json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def print_text_report(summary: dict[str, Any], evaluated: list[dict[str, Any]], *, hours: int) -> None:
    verdicts = Counter(row["verdict"] for row in evaluated)
    print(f"Shadow generation report ({hours}h lookback)")
    print(
        "Summary: "
        f"total={summary.get('total', 0)} "
        f"queued={summary.get('queued', 0)} "
        f"hitl_pending={summary.get('hitl_pending', 0)} "
        f"draft={summary.get('draft', 0)} "
        f"rejected={summary.get('rejected', 0)} "
        f"posted={summary.get('posted', 0)} "
        f"with_media={summary.get('with_media', 0)}"
    )
    print(
        "Verdicts in displayed rows: "
        f"PASS={verdicts.get('PASS', 0)} "
        f"REVIEW={verdicts.get('REVIEW', 0)} "
        f"BLOCK={verdicts.get('BLOCK', 0)} "
        f"INFO={verdicts.get('INFO', 0)}"
    )
    print(f"Last live post: {summary.get('last_posted_at')} ({age_label(summary.get('last_posted_at'))} ago)")
    print(f"Policy: ENABLE_REPLY_ENGAGEMENT={os.getenv('ENABLE_REPLY_ENGAGEMENT', 'false')}")
    print("")

    for row in evaluated:
        headline = row.get("headline") or "-"
        media = row["media"]
        reason_text = "; ".join(row["reasons"]) if row["reasons"] else "-"
        print(
            f"[{row['verdict']}] {row['status']} {row['id']} age={row['age']} "
            f"bucket={row.get('account_bucket')} pillar={row.get('pillar')} "
            f"media={media['media_kind']} reply_or_quote={row['is_reply_or_quote']}"
        )
        print(f"  reasons: {reason_text}")
        print(f"  source: {row.get('source') or '-'} / {row.get('category') or '-'} / {headline[:120]}")
        attention = row.get("attention") or {}
        print(
            f"  attention: grade={attention.get('grade')} score={attention.get('score')} "
            f"blockers={attention.get('blockers') or []}"
        )
        if row.get("posting_error"):
            print(f"  posting_error: {row['posting_error']}")
        print(f"  text: {row['content'][:280]}")
        print("")


def main() -> int:
    args = parse_args()
    load_runtime_env()
    statuses = tuple(args.status or POSTLIKE_STATUSES)

    with connect_db() as conn:
        summary = fetch_summary(conn, hours=args.hours)
        rows = fetch_shadow_rows(conn, hours=args.hours, limit=args.limit, statuses=statuses)

    evaluated = [evaluate_row(row) for row in rows]
    if args.json:
        print(json.dumps({"summary": summary, "rows": evaluated}, default=json_default, indent=2))
    else:
        print_text_report(summary, evaluated, hours=args.hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
