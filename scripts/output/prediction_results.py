#!/usr/bin/env python3
"""
Prediction Result Tracker — polls the prediction API for settled bets,
matches them to posted prediction tweets, and replies with the result.

Flow:
  1. Poll prediction API for all bets
  2. For each bet with result in (win, loss):
     a. Look up the prediction tweet in DB via bet_id
     b. Skip if already replied (result_tweet_id in event metadata)
     c. Generate a result card (green WIN / red LOSS) with running record
     d. Reply to the original prediction tweet
     e. Store the reply tweet ID in event metadata

Running record is computed live from the DB — no separate table needed.

PM2 process: prediction_results
Runs every 10 minutes.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import httpx
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
import tweepy

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.meme_generator import get_meme_generator
from processing.media_manager import get_media_manager
from utils.db_utils import ensure_db_connection
from utils.twitter_accounts import get_account_credentials

load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv('DATABASE_URL', '')
PREDICTION_API_URL = os.getenv(
    'PREDICTION_API_URL',
    'http://91.134.255.50/skinbetai/api/v3/bets/latest'
)
PREDICTION_API_KEY = os.getenv('PREDICTION_API_KEY', '')
POLL_INTERVAL = 600  # 10 minutes
DRY_RUN = os.getenv('DRY_RUN_MODE', 'false').lower() == 'true'


class PredictionResultTracker:
    def __init__(self):
        self.db_conn = None
        self.meme_generator = get_meme_generator()
        self.media_manager = get_media_manager()
        self.twitter_client = None
        self._init_twitter()

    def _init_twitter(self):
        """Initialize tweepy client for the main account."""
        creds = get_account_credentials('main')
        self.twitter_client = tweepy.Client(
            bearer_token=creds.get('bearer_token') or None,
            consumer_key=creds['api_key'],
            consumer_secret=creds['api_secret'],
            access_token=creds['access_token'],
            access_token_secret=creds['access_secret'],
            wait_on_rate_limit=True,
        )
        logger.info("✅ Twitter client initialized for result tracking")

    def _ensure_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    # ── API Polling ─────────────────────────────────────────────────

    def _fetch_bets(self) -> List[Dict[str, Any]]:
        """Fetch latest bets from the prediction API."""
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.get(
                    PREDICTION_API_URL,
                    headers={'X-API-Key': PREDICTION_API_KEY},
                )
                resp.raise_for_status()
                data = resp.json()
                bets = data if isinstance(data, list) else data.get('bets', data.get('data', []))
                return bets
        except Exception as e:
            logger.error(f"❌ Failed to fetch bets: {e}")
            return []

    # ── DB Lookups ──────────────────────────────────────────────────

    def _find_prediction_tweet(self, bet_id: str) -> Optional[Dict[str, Any]]:
        """Find the posted prediction tweet for a given bet_id.
        Returns dict with event_id, twitter_tweet_id, metadata, pick, opponent, etc."""
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT e.id, e.metadata, t.twitter_tweet_id, t.account_bucket
                    FROM twitter_bot.events e
                    JOIN twitter_bot.tweets_v2 t ON t.event_id = e.id
                    WHERE e.category = 'match_prediction'
                      AND e.metadata->>'bet_id' = %s
                      AND t.status = 'posted'
                      AND t.twitter_tweet_id IS NOT NULL
                    LIMIT 1
                """, (bet_id,))
                row = cur.fetchone()
                if not row:
                    return None
                meta = row[1] if isinstance(row[1], dict) else {}
                return {
                    'event_id': row[0],
                    'metadata': meta,
                    'twitter_tweet_id': row[2],
                    'account_bucket': row[3] or 'main',
                    'pick': meta.get('pick', ''),
                    'opponent': meta.get('team2', ''),
                    'confidence': meta.get('confidence', 'standard'),
                    'win_probability': float(meta.get('win_probability', 0) or 0),
                    'edge_pct': float(meta.get('edge_pct', 0) or 0),
                    'pick_odds': float(meta.get('pick_odds', 0) or 0),
                    'event_name': meta.get('event', ''),
                    'market_type': meta.get('market_type', 'match_winner'),
                }
        except Exception as e:
            logger.error(f"❌ DB lookup failed for bet {bet_id}: {e}")
            return None

    def _already_replied(self, event_id: str) -> bool:
        """Check if we already posted a result reply for this prediction."""
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM twitter_bot.events
                    WHERE id = %s
                      AND metadata ? 'result_tweet_id'
                    LIMIT 1
                """, (event_id,))
                return cur.fetchone() is not None
        except Exception as e:
            logger.error(f"❌ Reply check failed: {e}")
            return True  # Fail safe — don't double-post

    def _get_running_record(self) -> Dict[str, int]:
        """Compute W-L-V from all prediction events that have a result stored."""
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COALESCE(SUM(CASE WHEN metadata->>'bet_result' = 'win' THEN 1 ELSE 0 END), 0) as wins,
                        COALESCE(SUM(CASE WHEN metadata->>'bet_result' = 'loss' THEN 1 ELSE 0 END), 0) as losses,
                        COALESCE(SUM(CASE WHEN metadata->>'bet_result' = 'void' THEN 1 ELSE 0 END), 0) as voids
                    FROM twitter_bot.events
                    WHERE category = 'match_prediction'
                      AND metadata ? 'bet_result'
                """)
                row = cur.fetchone()
                return {
                    'wins': row[0] if row else 0,
                    'losses': row[1] if row else 0,
                    'voids': row[2] if row else 0,
                }
        except Exception as e:
            logger.error(f"❌ Running record query failed: {e}")
            return {'wins': 0, 'losses': 0, 'voids': 0}

    def _store_result(self, event_id: str, result: str, result_tweet_id: str = None):
        """Store the bet result and reply tweet ID in event metadata."""
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                update_parts = {"bet_result": result}
                if result_tweet_id:
                    update_parts["result_tweet_id"] = result_tweet_id
                cur.execute("""
                    UPDATE twitter_bot.events
                    SET metadata = metadata || %s::jsonb
                    WHERE id = %s
                """, (Json(update_parts), event_id))
                self.db_conn.commit()
                logger.info(f"📝 Stored result={result} for event {str(event_id)[:12]}")
        except Exception as e:
            logger.error(f"❌ Failed to store result: {e}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass

    # ── Tweet Formatting ────────────────────────────────────────────

    def _format_result_tweet(self, pred: Dict[str, Any], result: str,
                              record: Dict[str, int]) -> str:
        """Build a casual result tweet text."""
        pick = pred['pick']
        opponent = pred['opponent']
        odds = pred.get('pick_odds', 0)
        market = pred.get('market_type', 'match_winner')

        w, l = record['wins'], record['losses']
        # The current result is already counted in the record
        record_str = f"Record: {w}-{l}"

        if result == 'win':
            import hashlib
            seed = int(hashlib.md5(f"{pick}{opponent}".encode()).hexdigest()[:8], 16)
            templates = [
                f"Called it. {pick} cashes \u2705\n\n{record_str}",
                f"{pick} gets it done \u2705\n\n{record_str}",
                f"Another one. {pick} \u2705\n\n{record_str}",
                f"{pick} \u2705 Easy.\n\n{record_str}",
                f"told yall. {pick} wins \u2705\n\n{record_str}",
            ]
            if odds and odds >= 2.0:
                templates.append(f"{pick} @ {odds:.2f} \u2705\n\nFat odds cashed. {record_str}")
            tweet = templates[seed % len(templates)]
        else:  # loss
            import hashlib
            seed = int(hashlib.md5(f"{pick}{opponent}".encode()).hexdigest()[:8], 16)
            templates = [
                f"{pick} couldn't close it out \u274c\n\nTaking the L. {record_str}",
                f"Missed that one. {pick} \u274c\n\n{record_str}",
                f"Nope. {pick} falls \u274c\n\n{record_str}",
                f"{pick} lets us down. On to the next \u274c\n\n{record_str}",
                f"L on the {pick} pick \u274c\n\n{record_str}",
            ]
            tweet = templates[seed % len(templates)]

        return tweet.strip()

    # ── Card Generation ─────────────────────────────────────────────

    def _generate_result_card(self, pred: Dict[str, Any], result: str,
                               record: Dict[str, int]) -> Optional[str]:
        """Generate a WIN/LOSS result card image."""
        return self.meme_generator.generate_prediction_result_card(
            pick_team=pred['pick'],
            opponent=pred['opponent'],
            result=result,
            win_probability=pred.get('win_probability', 0),
            pick_odds=pred.get('pick_odds', 0),
            confidence=pred.get('confidence', 'standard'),
            event_name=pred.get('event_name', ''),
            market_type=pred.get('market_type', 'match_winner'),
            record_wins=record['wins'],
            record_losses=record['losses'],
        )

    # ── Reply Posting ───────────────────────────────────────────────

    def _post_reply(self, tweet_text: str, reply_to_id: str,
                     media_path: Optional[str] = None,
                     bucket: str = 'main') -> Optional[str]:
        """Post a reply to the original prediction tweet."""
        if DRY_RUN:
            logger.info(f"🧪 DRY RUN: Would reply to {reply_to_id}: {tweet_text[:80]}...")
            return "dry_run_result_reply"

        try:
            kwargs = {
                'text': tweet_text,
                'in_reply_to_tweet_id': reply_to_id,
            }
            if media_path:
                media_id = self.media_manager.upload_media(media_path, account_bucket=bucket)
                if media_id:
                    kwargs['media_ids'] = [media_id]

            resp = self.twitter_client.create_tweet(**kwargs)
            tweet_id = resp.data['id']
            logger.info(f"✅ Posted result reply: {tweet_id} → {reply_to_id}")
            return tweet_id
        except tweepy.TweepyException as e:
            err_str = str(e)
            if any(code in err_str for code in ('402', '429', '503')):
                logger.warning(f"⏸️  Rate/billing limit — will retry next cycle")
                raise  # Let the main loop handle backoff
            logger.error(f"❌ Failed to post result reply: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Unexpected error posting reply: {e}")
            return None

    # ── Main Loop ───────────────────────────────────────────────────

    def process_results(self):
        """Check all bets for settled results and post follow-ups."""
        bets = self._fetch_bets()
        if not bets:
            return

        settled = [b for b in bets if b.get('result') in ('win', 'loss')]
        if not settled:
            logger.info("📊 No settled bets to process")
            return

        logger.info(f"📊 Found {len(settled)} settled bets to check")

        for bet in settled:
            bet_id = bet.get('id') or bet.get('bet_id')
            result = bet['result']

            if not bet_id:
                logger.warning(f"⚠️  Settled bet missing ID, skipping")
                continue

            # Find the original prediction tweet
            pred = self._find_prediction_tweet(bet_id)
            if not pred:
                logger.debug(f"⏭️  No posted tweet for bet {bet_id}")
                continue

            # Already replied?
            if self._already_replied(pred['event_id']):
                logger.debug(f"⏭️  Already replied for bet {bet_id}")
                continue

            logger.info(
                f"🎯 Processing result: {result.upper()} for {pred['pick']} "
                f"vs {pred['opponent']} (bet {bet_id})"
            )

            # Store result first (so record is accurate)
            self._store_result(pred['event_id'], result)

            # Get running record (includes this result now)
            record = self._get_running_record()
            logger.info(f"📊 Running record: {record['wins']}W - {record['losses']}L")

            # Generate card
            card_path = self._generate_result_card(pred, result, record)
            if card_path:
                logger.info(f"🎨 Result card: {card_path}")

            # Format tweet
            tweet_text = self._format_result_tweet(pred, result, record)

            # Post reply to original prediction tweet
            try:
                reply_id = self._post_reply(
                    tweet_text=tweet_text,
                    reply_to_id=pred['twitter_tweet_id'],
                    media_path=card_path,
                    bucket=pred.get('account_bucket', 'main'),
                )
                if reply_id:
                    # Update metadata with result tweet ID
                    self._store_result(pred['event_id'], result, reply_id)
                    logger.info(
                        f"✅ Result posted: {result.upper()} — "
                        f"{pred['pick']} vs {pred['opponent']} — reply {reply_id}"
                    )
            except tweepy.TweepyException:
                # Rate/billing — stop processing, will retry next cycle
                logger.warning("⏸️  Pausing result processing due to rate limit")
                break

        # Also store results for void bets (no tweet, just record)
        voids = [b for b in bets if b.get('result') == 'void']
        for bet in voids:
            bet_id = bet.get('id') or bet.get('bet_id')
            if not bet_id:
                continue
            pred = self._find_prediction_tweet(bet_id)
            if pred and not self._already_replied(pred['event_id']):
                self._store_result(pred['event_id'], 'void')
                logger.info(f"📝 Stored void result for {pred['pick']} (no tweet)")


def main():
    tracker = PredictionResultTracker()
    logger.info("🚀 Prediction Result Tracker started")
    logger.info(f"   Poll interval: {POLL_INTERVAL}s | Dry run: {DRY_RUN}")

    while True:
        try:
            tracker.process_results()
        except Exception as e:
            logger.error(f"❌ Cycle error: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
