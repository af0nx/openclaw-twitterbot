#!/usr/bin/env python3
"""
Prediction Webhook Receiver — lightweight HTTP server.

Receives real-time bet predictions from the prediction VPS and
inserts them into the event pipeline for tweet generation.

Also polls the prediction API as a fallback to catch any
missed webhooks.

Architecture:
  Prediction VPS ──POST──▶ /webhook/prediction ──▶ twitter_bot.events
                                                      (category='match_prediction')
  Prediction API ◀──GET── poll every 5 min ──────▶ twitter_bot.events

Endpoints:
  POST /webhook/prediction  — receive new bet(s)
  GET  /health              — healthcheck
"""

import asyncio
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, Any, List

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.cs2_constants import T1_TEAMS, _has_t1_team
from utils.db_utils import ensure_db_connection

load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────
WEBHOOK_PORT = int(os.getenv('PREDICTION_WEBHOOK_PORT', '3336'))
WEBHOOK_SECRET = os.getenv('PREDICTION_WEBHOOK_SECRET', '')
DATABASE_URL = os.getenv('DATABASE_URL', '')

# Prediction API (polling fallback)
PREDICTION_API_URL = os.getenv(
    'PREDICTION_API_URL',
    'http://91.134.255.50/skinbetai/api/v3/bets/latest'
)
PREDICTION_API_KEY = os.getenv('PREDICTION_API_KEY', '')
POLL_INTERVAL = int(os.getenv('PREDICTION_POLL_INTERVAL', '300'))  # 5 min


def _is_tweetworthy(bet: Dict[str, Any]) -> bool:
    """All predictions are tweetworthy — we post every pick."""
    return True


def _is_bet_safe(bet: Dict[str, Any]) -> bool:
    """Reject void/settled/stale bets. Only accept unsettled, future matches."""
    # 1) Result must be absent or pending
    result = bet.get('result')
    if result and result not in ('pending',):
        logger.info(f"⛔ Skipping settled bet {bet.get('id')}: result={result}")
        return False

    # 2) Match date must be today or future (not already played)
    match_date_str = bet.get('date') or bet.get('match_date') or ''
    if match_date_str:
        try:
            match_date = datetime.strptime(str(match_date_str)[:10], '%Y-%m-%d').date()
            today = datetime.now(timezone.utc).date()
            if match_date < today:
                logger.info(
                    f"⛔ Skipping stale bet {bet.get('id')}: "
                    f"match_date={match_date} < today={today}"
                )
                return False
        except (ValueError, TypeError):
            pass  # Can't parse date — allow it but log

    return True


class PredictionWebhook:
    def __init__(self):
        self.db_conn = None
        self._seen_ids: set = set()

    def _ensure_db(self):
        self.db_conn = ensure_db_connection(self.db_conn, DATABASE_URL)

    def _bet_already_exists(self, bet_id: str) -> bool:
        if bet_id in self._seen_ids:
            return True
        dedup_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"prediction:{bet_id}"))
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM twitter_bot.events WHERE id = %s LIMIT 1",
                    (dedup_uuid,),
                )
                exists = cur.fetchone() is not None
                if exists:
                    self._seen_ids.add(bet_id)
                return exists
        except Exception:
            return False

    def _insert_prediction_event(self, bet: Dict[str, Any]) -> bool:
        """Insert a prediction bet into the events table."""
        bet_id = bet.get('id', '')
        if self._bet_already_exists(bet_id):
            logger.info(f"⏭️  Prediction {bet_id} already exists, skipping")
            return False

        team_a = bet.get('team_a', 'Unknown')
        team_b = bet.get('team_b', 'Unknown')
        pick = bet.get('pick', '')
        opponent = team_b if pick == team_a else team_a
        confidence = bet.get('confidence', 'standard')
        win_prob = bet.get('win_probability', 0)
        edge = bet.get('edge_pct', 0)
        event_name = bet.get('event', '')
        fmt = bet.get('format', '')
        market = bet.get('market_type', 'match_winner')

        headline = f"Prediction: {pick} over {opponent} ({confidence})"
        if market != 'match_winner':
            market_label = market.replace('_', ' ').title()
            headline = f"Prediction: {pick} [{market_label}] vs {opponent} ({confidence})"

        # Extract meaningful analysis factors (skip baselines)
        factors: List[str] = []
        if bet.get('analysis') and bet['analysis'].get('factors'):
            for f in bet['analysis']['factors']:
                f_lower = f.lower()
                if any(skip in f_lower for skip in (
                    'baseline', '+0.0%', 'skipped', 'adj disabled',
                    'even)', 'stable)', '0 flags',
                )):
                    continue
                factors.append(f)
            factors = factors[:8]  # Store more — tweet formatter picks the best

        content_parts = [
            f"{team_a} vs {team_b}",
            f"Pick: {pick} @ {bet.get('pick_odds', '?')}",
            f"Win probability: {win_prob:.1%}" if win_prob else "",
            f"Edge: {edge}%" if edge else "",
            f"Confidence: {confidence}",
            f"Format: {fmt}" if fmt else "",
            f"Event: {event_name}" if event_name else "",
        ]
        if factors:
            content_parts.append(f"Key factors: {'; '.join(factors)}")
        content = '\n'.join(p for p in content_parts if p)

        metadata = {
            'team1': pick,          # team1 = picked team
            'team2': opponent,      # team2 = opponent
            'team_a': team_a,
            'team_b': team_b,
            'pick': pick,
            'pick_odds': bet.get('pick_odds'),
            'confidence': confidence,
            'edge_pct': edge,
            'win_probability': win_prob,
            'event': event_name,
            'format': fmt,
            'market_type': market,
            'factors': factors,
            'match_id': bet.get('match_id', ''),
            'bet_id': bet_id,
        }

        try:
            self._ensure_db()
            dedup_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"prediction:{bet_id}"))
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO twitter_bot.events
                    (id, siftly_event_id, headline, content, category,
                     urgency, status, metadata, source, source_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    dedup_uuid,
                    None,
                    headline,
                    content,
                    'match_prediction',
                    'normal',
                    'pending',
                    Json(metadata),
                    'prediction_model',
                    f"http://91.134.255.50/skinbetai/api/v3/bets/{bet_id}",
                ))
                self.db_conn.commit()

            self._seen_ids.add(bet_id)
            logger.info(f"✅ Prediction event created: {pick} over {opponent} ({confidence})")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to insert prediction: {e}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass
            return False

    # ── HTTP Handlers ─────────────────────────────────────────────

    async def handle_webhook(self, request: Request) -> JSONResponse:
        """Handle incoming prediction webhook POST."""
        secret = request.headers.get('X-Webhook-Secret', '')
        if not WEBHOOK_SECRET:
            logger.error("❌ PREDICTION_WEBHOOK_SECRET not configured")
            return JSONResponse({'error': 'server misconfigured'}, status_code=500)

        if not secrets.compare_digest(secret, WEBHOOK_SECRET):
            logger.warning(f"⚠️  Unauthorized webhook attempt from {request.client.host}")
            return JSONResponse({'error': 'unauthorized'}, status_code=401)

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({'error': 'invalid JSON'}, status_code=400)

        # Support both single bet and array of bets
        bets = payload.get('bets', [payload] if 'id' in payload else [])

        inserted = 0
        for bet in bets:
            if not bet.get('id') or not bet.get('pick'):
                continue
            if not _is_bet_safe(bet):
                continue
            if not _is_tweetworthy(bet):
                logger.info(
                    f"⏭️  Skipping non-T1 prediction: "
                    f"{bet.get('team_a')} vs {bet.get('team_b')}"
                )
                continue
            if self._insert_prediction_event(bet):
                inserted += 1

        return JSONResponse({
            'status': 'ok',
            'inserted': inserted,
            'total': len(bets),
        })

    async def handle_health(self, request: Request) -> JSONResponse:
        return JSONResponse({'status': 'healthy'})

    # ── Polling Fallback ──────────────────────────────────────────

    async def _poll_predictions(self):
        """Fetch latest predictions from the API periodically."""
        if not PREDICTION_API_KEY or not PREDICTION_API_URL:
            logger.info("📡 Polling disabled — no PREDICTION_API_KEY/URL configured")
            return

        logger.info(f"📡 Prediction polling started (interval: {POLL_INTERVAL}s)")

        while True:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(
                        f"{PREDICTION_API_URL}?limit=5",
                        headers={'X-API-Key': PREDICTION_API_KEY},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        bets = data.get('bets', [])
                        for bet in bets:
                            if not bet.get('id') or not bet.get('pick'):
                                continue
                            if not _is_bet_safe(bet):
                                continue
                            if _is_tweetworthy(bet):
                                self._insert_prediction_event(bet)
                    else:
                        logger.warning(f"⚠️  Prediction API returned {resp.status_code}")
            except Exception as e:
                logger.warning(f"⚠️  Prediction poll error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

    # ── Startup ───────────────────────────────────────────────────

    async def _on_startup(self):
        logger.info(f"🚀 Prediction webhook listening on port {WEBHOOK_PORT}")
        # Start polling in background
        asyncio.create_task(self._poll_predictions())

    def create_app(self) -> Starlette:
        routes = [
            Route('/webhook/prediction', self.handle_webhook, methods=['POST']),
            Route('/health', self.handle_health, methods=['GET']),
        ]

        webhook_self = self

        @asynccontextmanager
        async def lifespan(app):
            await webhook_self._on_startup()
            yield

        app = Starlette(routes=routes, lifespan=lifespan)
        return app


webhook = PredictionWebhook()
app = webhook.create_app()


if __name__ == '__main__':
    uvicorn.run(
        'ingestion.prediction_webhook:app',
        host='0.0.0.0',
        port=WEBHOOK_PORT,
        log_level='info',
    )
