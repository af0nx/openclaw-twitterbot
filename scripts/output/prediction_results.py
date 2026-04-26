#!/usr/bin/env python3
"""Prediction Result Tracker backed by our own `match_result` events.

This no longer talks to the external signal API. It resolves posted
`match_prediction` tweets against match results already stored in
`twitter_bot.events` and replies when the prediction settles.
"""

import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Dict, Any, Optional, List

from dotenv import load_dotenv
from psycopg2.extras import Json
import tweepy

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.meme_generator import get_meme_generator
from processing.media_manager import get_media_manager
from processing.team_rating_engine import canonicalize_team_name
from utils.db_utils import ensure_db_connection
from utils.twitter_accounts import get_account_credentials

load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv('DATABASE_URL', '')
POLL_INTERVAL = 600
DRY_RUN = os.getenv('DRY_RUN_MODE', 'false').lower() == 'true'
SHUTDOWN_EVENT = Event()


def request_shutdown(_signum=None, _frame=None):
    logger.info("⏹️  Prediction Result Tracker shutdown requested")
    SHUTDOWN_EVENT.set()


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

    # ── DB Lookups ──────────────────────────────────────────────────

    def _find_open_predictions(self) -> List[Dict[str, Any]]:
        """Find posted predictions that do not yet have a result reply."""
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT e.id, e.created_at, e.metadata, t.twitter_tweet_id, t.account_bucket
                    FROM twitter_bot.events e
                    JOIN twitter_bot.tweets_v2 t ON t.event_id = e.id
                    WHERE e.category = 'match_prediction'
                      AND t.status = 'posted'
                      AND t.twitter_tweet_id IS NOT NULL
                      AND NOT (e.metadata ? 'result_tweet_id')
                    ORDER BY e.created_at DESC
                    LIMIT 100
                """)
                rows = cur.fetchall()

            predictions = []
            for row in rows:
                meta = row[2] if isinstance(row[2], dict) else {}
                predictions.append({
                    'event_id': row[0],
                    'created_at': row[1],
                    'metadata': meta,
                    'twitter_tweet_id': row[3],
                    'account_bucket': row[4] or 'main',
                    'pick': meta.get('pick', ''),
                    'opponent': meta.get('team2', ''),
                    'team_a': meta.get('team_a') or meta.get('pick', ''),
                    'team_b': meta.get('team_b') or meta.get('team2', ''),
                    'hltv_match_id': meta.get('hltv_match_id') or meta.get('match_id'),
                    'confidence': meta.get('confidence', 'standard'),
                    'win_probability': float(meta.get('win_probability', 0) or 0),
                    'edge_pct': float(meta.get('edge_pct', 0) or 0),
                    'pick_odds': float(meta.get('pick_odds', 0) or 0),
                    'event_name': meta.get('event', ''),
                    'market_type': meta.get('market_type', 'match_winner'),
                })
            return predictions
        except Exception as e:
            logger.error(f"❌ Failed to load open predictions: {e}")
            return []

    def _find_result_by_match_id(self, hltv_match_id: str, prediction_created_at: datetime) -> Optional[Dict[str, Any]]:
        if not hltv_match_id:
            return None

        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, metadata, created_at
                    FROM twitter_bot.events
                    WHERE category = 'match_result'
                      AND metadata->>'hltv_match_id' = %s
                      AND created_at > %s - INTERVAL '12 hours'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (str(hltv_match_id), prediction_created_at),
                )
                row = cur.fetchone()
            if not row:
                return None
            return {
                'event_id': row[0],
                'metadata': row[1] if isinstance(row[1], dict) else {},
                'created_at': row[2],
            }
        except Exception as e:
            logger.error(f"❌ Exact match-result lookup failed for HLTV match {hltv_match_id}: {e}")
            return None

    def _find_result_by_matchup(self, pred: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        pred_keys = {
            canonicalize_team_name(pred.get('team_a')),
            canonicalize_team_name(pred.get('team_b')),
        }
        pred_keys.discard(None)
        if len(pred_keys) != 2:
            return None

        lower_bound = max(
            pred['created_at'] - timedelta(hours=6),
            datetime.now(timezone.utc) - timedelta(days=7),
        )

        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, metadata, created_at
                    FROM twitter_bot.events
                    WHERE category = 'match_result'
                      AND created_at > %s
                    ORDER BY created_at DESC
                    LIMIT 100
                    """,
                    (lower_bound,),
                )
                rows = cur.fetchall()
        except Exception as e:
            logger.error(f"❌ Matchup result lookup failed for {pred.get('pick')}: {e}")
            return None

        for row in rows:
            meta = row[1] if isinstance(row[1], dict) else {}
            result_keys = {
                canonicalize_team_name(meta.get('team1')),
                canonicalize_team_name(meta.get('team2')),
            }
            result_keys.discard(None)
            if result_keys == pred_keys:
                return {
                    'event_id': row[0],
                    'metadata': meta,
                    'created_at': row[2],
                }
        return None

    def _find_settled_result(self, pred: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        exact = self._find_result_by_match_id(pred.get('hltv_match_id'), pred['created_at'])
        if exact:
            return exact
        return self._find_result_by_matchup(pred)

    @staticmethod
    def _determine_result(pred: Dict[str, Any], result_event: Dict[str, Any]) -> Optional[str]:
        metadata = result_event.get('metadata') or {}
        winner_key = canonicalize_team_name(metadata.get('winner'))
        pick_key = canonicalize_team_name(pred.get('pick'))
        if not winner_key or not pick_key:
            return None
        return 'win' if winner_key == pick_key else 'loss'

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

    def _store_result(
        self,
        event_id: str,
        result: str,
        result_tweet_id: str = None,
        resolved_match_event_id: str = None,
    ):
        """Store the bet result and reply tweet ID in event metadata."""
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                update_parts = {"bet_result": result}
                if result_tweet_id:
                    update_parts["result_tweet_id"] = result_tweet_id
                if resolved_match_event_id:
                    update_parts["resolved_match_event_id"] = str(resolved_match_event_id)
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
        """Resolve posted predictions against our own stored match results."""
        predictions = self._find_open_predictions()
        if not predictions:
            logger.info("📭 No open prediction replies to resolve")
            return

        logger.info(f"📊 Checking {len(predictions)} open predictions against in-house match results")

        for pred in predictions:
            if self._already_replied(pred['event_id']):
                continue

            result_event = self._find_settled_result(pred)
            if not result_event:
                continue

            result = self._determine_result(pred, result_event)
            if result not in ('win', 'loss'):
                continue

            logger.info(
                f"🎯 Processing result: {result.upper()} for {pred['pick']} vs "
                f"{pred['opponent']} (match event {str(result_event['event_id'])[:12]})"
            )

            self._store_result(
                pred['event_id'],
                result,
                resolved_match_event_id=result_event['event_id'],
            )

            record = self._get_running_record()
            logger.info(f"📊 Running record: {record['wins']}W - {record['losses']}L")

            card_path = self._generate_result_card(pred, result, record)
            if card_path:
                logger.info(f"🎨 Result card: {card_path}")

            tweet_text = self._format_result_tweet(pred, result, record)

            try:
                reply_id = self._post_reply(
                    tweet_text=tweet_text,
                    reply_to_id=pred['twitter_tweet_id'],
                    media_path=card_path,
                    bucket=pred.get('account_bucket', 'main'),
                )
                if reply_id:
                    self._store_result(
                        pred['event_id'],
                        result,
                        result_tweet_id=reply_id,
                        resolved_match_event_id=result_event['event_id'],
                    )
                    logger.info(
                        f"✅ Result posted: {result.upper()} — {pred['pick']} vs "
                        f"{pred['opponent']} — reply {reply_id}"
                    )
            except tweepy.TweepyException:
                logger.warning("⏸️  Pausing result processing due to rate limit")
                break


def main():
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_shutdown)

    tracker = PredictionResultTracker()
    logger.info("🚀 Prediction Result Tracker started")
    logger.info(f"   Poll interval: {POLL_INTERVAL}s | Dry run: {DRY_RUN}")

    try:
        while not SHUTDOWN_EVENT.is_set():
            try:
                tracker.process_results()
            except Exception as e:
                logger.error(f"❌ Cycle error: {e}", exc_info=True)

            if SHUTDOWN_EVENT.wait(POLL_INTERVAL):
                break
    finally:
        if tracker.db_conn:
            tracker.db_conn.close()


if __name__ == '__main__':
    main()
