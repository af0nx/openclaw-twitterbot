#!/usr/bin/env python3
"""Send daily and urgent Telegram operational status for the CS2 bot."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = Path("/dev/shm/.env")
DEFAULT_STATE_FILE = REPO_ROOT / "logs" / "daily-status-report-state.json"
TELEGRAM_LIMIT = 4096

REQUIRED_SERVICES = (
    "scrapling_pool",
    "tweet_scheduler",
    "twitter_poster",
    "vip_hitl_bot",
    "prediction_webhook",
    "prediction_results",
    "siftly_ingestor",
    "engagement_tracker",
    "follower_growth",
    "style_scraper",
    "dashboard",
)

OPTIONAL_SERVICES = (
    "clip_hunter",
    "tweet_pruner",
    "engagement_engine",
    "community_liker",
    "live_watcher",
    "ab_evaluator",
    "ml_retrain",
)

STALE_SUCCESS_THRESHOLDS_HOURS = {
    "tweet_scheduler": 6,
    "twitter_poster": 12,
    "engagement_tracker": 8,
    "siftly_ingestor": 24,
}

RESTART_WARNING_THRESHOLD = 20
RESTART_CRITICAL_THRESHOLD = 100
RESTART_RECENT_WINDOW_HOURS = 1
RESTART_WARNING_STABLE_WINDOW_HOURS = 24
DEFAULT_ALERT_COOLDOWN_HOURS = 6

SECRET_PATTERNS = (
    (
        re.compile(r"(postgres(?:ql)?://)([^:@/\s]+):([^@\s]+)@", re.IGNORECASE),
        r"\1<redacted>:<redacted>@",
    ),
    (re.compile(r"(://[^:/\s]+:)([^@\s]+)@"), r"\1<redacted>@"),
    (re.compile(r"bot\d+:[A-Za-z0-9_-]+"), "bot<redacted>"),
    (re.compile(r"sk-(?:or-)?[A-Za-z0-9_-]{8,}"), "sk-<redacted>"),
    (
        re.compile(
            r"((?:TOKEN|SECRET|KEY|PASSWORD|DATABASE_URL|WEBHOOK_SECRET)[A-Z0-9_]*=)[^\s]+",
            re.IGNORECASE,
        ),
        r"\1<redacted>",
    ),
)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def redact_secret(value: Any) -> str:
    text = str(value)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def safe_error(exc: BaseException) -> str:
    return redact_secret(f"{exc.__class__.__name__}: {exc}")


def load_env_file(path: Path = DEFAULT_ENV_FILE) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_timestamp(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        parsed = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            timestamp = float(text)
            if timestamp > 10_000_000_000:
                timestamp = timestamp / 1000
            parsed = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def hours_since(value: Any, now: dt.datetime) -> float | None:
    parsed = parse_timestamp(value)
    if not parsed:
        return None
    return max((now - parsed).total_seconds() / 3600, 0.0)


def format_age(value: Any, now: dt.datetime) -> str:
    parsed = parse_timestamp(value)
    if not parsed:
        return "never"
    age = hours_since(parsed, now)
    return f"{parsed:%Y-%m-%d %H:%M UTC} ({age:.1f}h ago)"


def format_duration_hours(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 1:
        return f"{value * 60:.0f}m"
    if value < 48:
        return f"{value:.1f}h"
    return f"{value / 24:.1f}d"


def _load_status_dashboard_module():
    try:
        from scripts.ops import status_dashboard

        return status_dashboard
    except Exception:
        import status_dashboard

        return status_dashboard


def collect_pm2() -> tuple[list[dict[str, Any]], str | None]:
    try:
        completed = subprocess.run(
            ["pm2", "jlist"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        rows = []
        for item in json.loads(completed.stdout):
            env = item.get("pm2_env", {}) or {}
            rows.append(
                {
                    "name": item.get("name"),
                    "status": env.get("status", "unknown"),
                    "restart_count": parse_int(env.get("restart_time")),
                    "memory_bytes": parse_int((item.get("monit") or {}).get("memory")),
                    "pid": parse_int(item.get("pid")),
                    "pm_uptime": env.get("pm_uptime"),
                }
            )
        return rows, None
    except Exception as exc:
        return [], f"pm2 jlist failed: {safe_error(exc)}"


def collect_dashboard() -> dict[str, Any]:
    dashboard: dict[str, Any] = {"errors": []}
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        dashboard["errors"].append("DATABASE_URL is not set")
    else:
        try:
            import psycopg2

            status_dashboard = _load_status_dashboard_module()
            conn = psycopg2.connect(database_url)
            try:
                dashboard.update(status_dashboard.build_dashboard(conn))
            finally:
                conn.close()
        except Exception as exc:
            dashboard["errors"].append(f"database/status dashboard failed: {safe_error(exc)}")

    pm2_rows, pm2_error = collect_pm2()
    if pm2_rows:
        dashboard["pm2"] = pm2_rows
    else:
        dashboard.setdefault("pm2", [])
    if pm2_error:
        dashboard["errors"].append(pm2_error)

    return dashboard


def service_map(dashboard: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("name")): row
        for row in dashboard.get("pm2", [])
        if row.get("name")
    }


def build_report(dashboard: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    critical: list[str] = []
    warnings: list[str] = []
    services = service_map(dashboard)

    for error in dashboard.get("errors", []):
        critical.append(f"status collection failed: {redact_secret(error)}")

    for name in REQUIRED_SERVICES:
        row = services.get(name)
        if not row:
            critical.append(f"{name} missing from PM2")
            continue

        status = row.get("status", "unknown")
        restarts = parse_int(row.get("restart_count"))
        uptime_age = hours_since(row.get("pm_uptime"), now)
        if status != "online":
            critical.append(f"{name} is {status}")
        elif restarts >= RESTART_CRITICAL_THRESHOLD and (
            uptime_age is None or uptime_age <= RESTART_RECENT_WINDOW_HOURS
        ):
            critical.append(f"{name} restarted recently with high lifetime restart count ({restarts})")
        elif restarts >= RESTART_CRITICAL_THRESHOLD:
            warnings.append(
                f"{name} has high historical restarts "
                f"({restarts}; stable {format_duration_hours(uptime_age)})"
            )
        elif restarts >= RESTART_WARNING_THRESHOLD and (
            uptime_age is None or uptime_age <= RESTART_WARNING_STABLE_WINDOW_HOURS
        ):
            warnings.append(f"{name} has {restarts} restarts")

    for name in OPTIONAL_SERVICES:
        row = services.get(name)
        if row and row.get("status") != "online":
            warnings.append(f"optional {name} is {row.get('status', 'unknown')}")

    for name, threshold_hours in STALE_SUCCESS_THRESHOLDS_HOURS.items():
        timestamp = (dashboard.get("service_last_success") or {}).get(name)
        age = hours_since(timestamp, now)
        if age is None:
            warnings.append(f"{name} has no recorded success timestamp")
        elif age > threshold_hours:
            warnings.append(
                f"{name} last success is stale ({age:.1f}h ago; threshold {threshold_hours}h)"
            )

    queue_depth = dashboard.get("queue_depth") or {}
    queued = parse_int(queue_depth.get("queued"))
    hitl_pending = parse_int(queue_depth.get("hitl_pending"))
    draft = parse_int(queue_depth.get("draft"))
    if queued > 25:
        warnings.append(f"queued tweet backlog is {queued}")
    if hitl_pending > 0:
        warnings.append(f"{hitl_pending} HITL approvals pending")
    if draft > 50:
        warnings.append(f"draft backlog is {draft}")

    required_online = sum(
        1 for name in REQUIRED_SERVICES if services.get(name, {}).get("status") == "online"
    )
    stopped_services = [
        name
        for name, row in services.items()
        if row.get("status") and row.get("status") != "online"
    ]

    severity = "CRITICAL" if critical else "WARNING" if warnings else "OK"
    return {
        "generated_at": now.isoformat(),
        "severity": severity,
        "critical": critical,
        "warnings": warnings,
        "stats": {
            "required_online": required_online,
            "required_total": len(REQUIRED_SERVICES),
            "stopped_services": sorted(stopped_services),
            "queue_depth": {
                "queued": queued,
                "hitl_pending": hitl_pending,
                "draft": draft,
            },
            "media_attach": dashboard.get("media_attach") or {},
            "approval": dashboard.get("approval") or {},
            "service_last_success": dashboard.get("service_last_success") or {},
        },
    }


def truncate_for_telegram(text: str) -> str:
    if len(text) <= TELEGRAM_LIMIT:
        return text
    suffix = "\n\n[truncated]"
    return f"{text[: TELEGRAM_LIMIT - len(suffix)]}{suffix}"


def render_message(report: dict[str, Any], dashboard: dict[str, Any] | None = None) -> str:
    dashboard = dashboard or {}
    now = parse_timestamp(report["generated_at"]) or utcnow()
    stats = report.get("stats", {})
    queue = stats.get("queue_depth", {})
    media = stats.get("media_attach", {})
    approval = stats.get("approval", {})
    last_success = stats.get("service_last_success", {})
    stopped = stats.get("stopped_services", [])

    lines = [
        f"SkinBetHub bot ops - {now:%Y-%m-%d %H:%M UTC}",
        f"Status: {report['severity']}",
        "",
    ]

    if report.get("critical"):
        lines.append("Critical:")
        lines.extend(f"- {item}" for item in report["critical"][:8])
        if len(report["critical"]) > 8:
            lines.append(f"- ...and {len(report['critical']) - 8} more")
        lines.append("")

    if report.get("warnings"):
        lines.append("Warnings:")
        lines.extend(f"- {item}" for item in report["warnings"][:10])
        if len(report["warnings"]) > 10:
            lines.append(f"- ...and {len(report['warnings']) - 10} more")
        lines.append("")

    if not report.get("critical") and not report.get("warnings"):
        lines.append("No script/system issues detected.")
        lines.append("")

    lines.extend(
        [
            "Activity:",
            (
                f"- Queue: queued={queue.get('queued', 0)}, "
                f"hitl_pending={queue.get('hitl_pending', 0)}, "
                f"draft={queue.get('draft', 0)}"
            ),
            (
                f"- Posted in 30d: {media.get('posted', 0)}; "
                f"media attach rate: {media.get('attach_rate', 0)}%"
            ),
            (
                f"- Approvals in 30d: auto={approval.get('auto_approved', 0)}, "
                f"requested={approval.get('hitl_requested', 0)}, "
                f"approved={approval.get('hitl_approved', 0)}"
            ),
            "",
            "Service timestamps:",
        ]
    )
    for service in STALE_SUCCESS_THRESHOLDS_HOURS:
        lines.append(f"- {service}: {format_age(last_success.get(service), now)}")

    lines.extend(
        [
            "",
            "PM2:",
            (
                f"- Required online: {stats.get('required_online', 0)}/"
                f"{stats.get('required_total', len(REQUIRED_SERVICES))}"
            ),
            f"- Stopped/non-online: {', '.join(stopped) if stopped else 'none'}",
        ]
    )

    if dashboard.get("bandit", {}).get("ready_to_tune") is False:
        lines.append("- Generator tuning: not enough samples yet")

    return truncate_for_telegram(redact_secret("\n".join(lines).strip()))


def alert_signature(report: dict[str, Any]) -> str:
    payload = "\n".join(sorted(normalize_alert_item(item) for item in report.get("critical") or []))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_alert_item(item: str) -> str:
    item = re.sub(r"\(\d+(?:\.\d+)?\)", "(n)", item)
    item = re.sub(r"\b\d+(?:\.\d+)?h\b", "nh", item)
    return item


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def should_send_alert(
    report: dict[str, Any],
    state: dict[str, Any],
    now: dt.datetime,
    cooldown_hours: float,
) -> tuple[bool, str]:
    if not report.get("critical"):
        return False, "no critical issues"

    signature = alert_signature(report)
    if state.get("last_alert_signature") != signature:
        return True, "critical issue set changed"

    last_sent = parse_timestamp(state.get("last_alert_at"))
    if not last_sent:
        return True, "no previous alert timestamp"

    age_hours = (now - last_sent).total_seconds() / 3600
    if age_hours >= cooldown_hours:
        return True, f"cooldown elapsed ({age_hours:.1f}h)"

    return False, f"cooldown active ({age_hours:.1f}h/{cooldown_hours:.1f}h)"


def parse_chat_ids(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def send_telegram_message(message: str) -> list[str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = parse_chat_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    if not chat_ids:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_IDS is not set")

    sent: list[str] = []
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        payload = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status >= 300:
                    raise RuntimeError(f"Telegram HTTP {response.status}")
        except Exception as exc:
            raise RuntimeError(f"Telegram send failed for chat {chat_id}: {safe_error(exc)}")
        sent.append(chat_id)
    return sent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send SkinBetHub bot operational status to Telegram",
    )
    parser.add_argument("--mode", choices=("daily", "alert"), default="daily")
    parser.add_argument("--dry-run", action="store_true", help="Print the message without sending")
    parser.add_argument("--json", action="store_true", help="Print structured result")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--cooldown-hours", type=float, default=DEFAULT_ALERT_COOLDOWN_HOURS)
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file(args.env_file)

    now = utcnow()
    dashboard = collect_dashboard()
    report = build_report(dashboard, now)
    message = render_message(report, dashboard)

    should_send = args.mode == "daily"
    suppression_reason = ""
    state: dict[str, Any] = {}

    if args.mode == "alert":
        state = load_state(args.state_file)
        should_send, suppression_reason = should_send_alert(
            report,
            state,
            now,
            args.cooldown_hours,
        )

    sent_to: list[str] = []
    send_error = ""
    if should_send and not args.dry_run:
        try:
            sent_to = send_telegram_message(message)
        except Exception as exc:
            send_error = safe_error(exc)
        else:
            if report.get("critical"):
                save_state(
                    args.state_file,
                    {
                        **state,
                        "last_alert_at": now.isoformat(),
                        "last_alert_signature": alert_signature(report),
                        "last_alert_summary": report.get("critical", []),
                    },
                )

    result = {
        "mode": args.mode,
        "severity": report["severity"],
        "critical": report["critical"],
        "warnings": report["warnings"],
        "sent": bool(sent_to),
        "sent_to": sent_to,
        "suppression_reason": suppression_reason,
        "send_error": send_error,
        "message": message,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    elif args.dry_run:
        print(message)
        if args.mode == "alert" and not should_send:
            print(f"\nAlert suppressed: {suppression_reason}")
    elif send_error:
        print(send_error, file=sys.stderr)

    return 1 if send_error else 0


if __name__ == "__main__":
    raise SystemExit(run())
