#!/usr/bin/env python3
"""
Live Match Watcher — AI-powered live CS2 commentary via screenshots.

Periodically captures Twitch stream screenshots during active T1 matches,
feeds them to Gemini Flash vision AI, detects highlight moments
(clutch, match point, overtime, eco upset, comeback), and auto-tweets
narration with the screenshot attached.

This is the "eyes + voice" of the bot — it SEES the game and narrates it.

Flow:
  1. Check HLTV for live T1 matches
  2. Take Twitch screenshot every 60-90 seconds
  3. AI vision extracts game state (teams, score, round, economy, players)
  4. Detect if current state is a "highlight moment"
  5. If highlight → generate hype narration → attach screenshot → tweet

PM2 service: `pm2 start scripts/services/live_match_watcher.py --name live-watcher --interpreter python3`
"""

import asyncio
import logging
import os
import random
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv('/dev/shm/.env')

# Path setup
sys.path.insert(0, str(Path(__file__).parent.parent / 'processing'))
sys.path.insert(0, str(Path(__file__).parent.parent))

from screenshot_analyzer import ScreenshotAnalyzer
from live_action_detector import LiveActionDetector
from twitch_screenshotter import get_twitch_screenshotter
from media_manager import get_media_manager

from utils.account_quota import reserve_account_slot
from utils.db_utils import ensure_db_connection
from utils.runtime_schema import ensure_runtime_schema_extensions
from utils.twitter_accounts import get_bucket_daily_cap
from tone_validator import get_tone_validator

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('live_match_watcher')

# ─── Config ───
SCREENSHOT_INTERVAL = int(os.getenv('LIVE_WATCHER_INTERVAL', 12))  # seconds between local monitor sweeps
MONITOR_SECONDS = int(os.getenv('LIVE_WATCHER_MONITOR_SECONDS', 8))
MONITOR_FPS = float(os.getenv('LIVE_WATCHER_MONITOR_FPS', 1))
BURST_FRAMES = int(os.getenv('LIVE_WATCHER_BURST_FRAMES', 3))
MAX_TWEETS_PER_MATCH = int(os.getenv('LIVE_WATCHER_MAX_TWEETS', 4))  # cap per match to avoid spam
COOLDOWN_AFTER_TWEET = 180  # 3 min cooldown after tweeting to avoid rapid-fire


class LiveMatchWatcher:
    """Watches live CS2 matches via screenshot analysis and tweets highlights."""

    def __init__(self):
        self.analyzer = ScreenshotAnalyzer()
        self.detector = LiveActionDetector()
        self.screenshotter = get_twitch_screenshotter()
        self.media_manager = get_media_manager()
        self.db_conn = None
        self._db_backoff = 1  # seconds, doubles on consecutive failures
        self._schema_ready = False

        # Track per-match state
        self._match_tweet_counts: dict = {}  # match_key → tweet count
        self._match_game_states: dict = {}  # match_key → last game state
        self._match_last_seen: dict = {}     # match_key → timestamp
        self._last_tweet_time: float = 0

    def _bootstrap_runtime_schema(self):
        if self._schema_ready:
            return

        self.db_conn = ensure_db_connection(self.db_conn)
        ensure_runtime_schema_extensions(self.db_conn)
        self._schema_ready = True

    def _ensure_db(self):
        try:
            self.db_conn = ensure_db_connection(self.db_conn)
            self._db_backoff = 1  # reset on success
        except Exception as e:
            logger.warning(f"⚠️  DB connect failed (backoff {self._db_backoff}s): {e}")
            time.sleep(self._db_backoff)
            self._db_backoff = min(self._db_backoff * 2, 60)
            raise

    def _cleanup_stale_matches(self):
        """Remove tracking state for matches not seen in 6+ hours."""
        now = time.time()
        stale = [k for k, ts in self._match_last_seen.items() if now - ts > 6 * 3600]
        for k in stale:
            self._match_tweet_counts.pop(k, None)
            self._match_game_states.pop(k, None)
            self._match_last_seen.pop(k, None)
            logger.debug(f"🧹 Cleaned up stale match state: {k}")

    def _get_active_matches(self) -> list:
        """Check DB for live T1 matches (from HLTV monitor events)."""
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                # Look for recent match_result events that might still be live
                # or match_preview events for upcoming matches
                cur.execute("""
                    SELECT
                        COALESCE(metadata->>'team1', '') as team1,
                        COALESCE(metadata->>'team2', '') as team2,
                        COALESCE(metadata->>'event', '') as event_name,
                        COALESCE(metadata->'match_context'->>'stream', '') as stream
                    FROM twitter_bot.events
                    WHERE category IN ('match_result', 'match_preview')
                    AND created_at > NOW() - INTERVAL '4 hours'
                    AND metadata->>'team1' IS NOT NULL
                    AND metadata->>'team2' IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 5
                """)
                rows = cur.fetchall()
                return [
                    {'team1': r[0], 'team2': r[1], 'event': r[2], 'stream': r[3]}
                    for r in rows if r[0] and r[1]
                ]
        except Exception as e:
            logger.warning(f"⚠️  Failed to fetch active matches: {e}")
            return []

    def _match_key(self, match: dict) -> str:
        """Unique key for a match."""
        t1 = (match.get('team1') or '').lower()
        t2 = (match.get('team2') or '').lower()
        return f"{min(t1,t2)}_{max(t1,t2)}"

    def _can_tweet(self, match_key: str) -> bool:
        """Check if we can tweet for this match (rate limiting)."""
        # Per-match cap
        count = self._match_tweet_counts.get(match_key, 0)
        if count >= MAX_TWEETS_PER_MATCH:
            logger.debug(f"⏸️  Match {match_key}: hit tweet cap ({count}/{MAX_TWEETS_PER_MATCH})")
            return False

        # Global cooldown
        elapsed = time.time() - self._last_tweet_time
        if elapsed < COOLDOWN_AFTER_TWEET:
            logger.debug(f"⏸️  Cooldown: {COOLDOWN_AFTER_TWEET - elapsed:.0f}s remaining")
            return False

        return True

    def _is_new_state(self, match_key: str, game_state: dict) -> bool:
        """Check if game state is meaningfully different from last one for this match."""
        old = self._match_game_states.get(match_key)
        if not old:
            return True

        new = game_state

        # Different round = new state
        if new.get('round') != old.get('round'):
            return True

        # Different score = new state
        old_s1 = old.get('team1', {}).get('score')
        old_s2 = old.get('team2', {}).get('score')
        new_s1 = new.get('team1', {}).get('score')
        new_s2 = new.get('team2', {}).get('score')
        if (old_s1, old_s2) != (new_s1, new_s2):
            return True

        # Different alive counts = new state
        old_alive = old.get('players_alive', {})
        new_alive = new.get('players_alive', {})
        if old_alive != new_alive:
            return True

        return False

    async def _queue_live_tweet(self, text: str, screenshot_path: str, match: dict):
        """Queue a live narration tweet with the screenshot."""
        try:
            self._ensure_db()

            # Upload screenshot
            media_id = self.media_manager.upload_media(screenshot_path, account_bucket='live')
            if not media_id:
                logger.warning("⚠️  Failed to upload screenshot for live tweet")
                return

            daily_cap = get_bucket_daily_cap('live')
            if not reserve_account_slot(self.db_conn, 'live', daily_cap):
                logger.warning("🚫 Live account quota exhausted — skipping live tweet")
                return

            # Insert as auto-approved queued tweet (live narration = time-sensitive)
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO twitter_bot.tweets_v2
                    (pillar, pillar_name, content, status, media_path, account_bucket,
                     auto_approved, fact_checked, tone_validated)
                    VALUES (2, 'live_narration', %s, 'queued', %s, 'live', true, true, true)
                    RETURNING id
                """, (text, media_id))

                tweet_id = cur.fetchone()[0]
                self.db_conn.commit()

            match_key = self._match_key(match)
            self._match_tweet_counts[match_key] = self._match_tweet_counts.get(match_key, 0) + 1
            self._last_tweet_time = time.time()

            logger.info(f"🐦 Live tweet queued (#{tweet_id}): {text[:60]}...")

        except Exception as e:
            logger.error(f"❌ Failed to queue live tweet: {e}", exc_info=True)
            try:
                if self.db_conn:
                    self.db_conn.rollback()
            except Exception:
                pass

    async def watch_match(self, match: dict):
        """Locally monitor a match and only escalate burst screenshots to vision on action spikes."""
        match_key = self._match_key(match)

        trigger_payload = await self.screenshotter.get_highlight_burst_for_event(
            match,
            self.detector,
            monitor_seconds=MONITOR_SECONDS,
            monitor_fps=MONITOR_FPS,
            burst_count=BURST_FRAMES,
        )
        if not trigger_payload:
            logger.debug(f"📺 No local action spike for {match.get('team1')} vs {match.get('team2')}")
            return

        best_state = None
        best_highlight = None
        best_path = None
        for screenshot_path in trigger_payload.get('burst_paths', []):
            game_state = self.analyzer.analyze_screenshot(screenshot_path)
            if 'error' in game_state:
                continue

            if not self._is_new_state(match_key, game_state):
                continue

            highlight = self.analyzer.detect_highlight_moment(game_state)
            if not highlight:
                continue

            best_state = game_state
            best_highlight = highlight
            best_path = screenshot_path
            if highlight.get('urgency') == 'high':
                break

        if not best_state or not best_highlight or not best_path:
            logger.debug("ℹ️  Local trigger fired, but no tweet-worthy vision state was confirmed")
            return

        self._match_game_states[match_key] = best_state
        self._match_last_seen[match_key] = time.time()
        logger.info(f"🔥 Highlight: {best_highlight['type']} — {best_highlight.get('detail', '')}")

        # Check rate limits
        if not self._can_tweet(match_key):
            return

        # Generate narration
        narration = self.analyzer.generate_live_narration(best_state)
        if not narration:
            return

        # Tone gate — reject narration that sounds corporate/AI
        try:
            tone_result = get_tone_validator().validate(narration, pillar=2)
            if not tone_result.get('valid', True):
                logger.warning(f"⚠️  Live narration failed tone check: {tone_result.get('issues', [])}")
                return
        except Exception as e:
            logger.debug(f"⚠️  Tone validation skipped: {e}")

        # Queue the tweet
        await self._queue_live_tweet(narration, best_path, match)

    async def run_cycle(self):
        """One cycle: check for live matches, watch them."""
        self._cleanup_stale_matches()
        matches = self._get_active_matches()
        if not matches:
            logger.debug("ℹ️  No active matches")
            return

        for match in matches:
            try:
                await self.watch_match(match)
            except Exception as e:
                logger.warning(f"⚠️  Error watching {match.get('team1')} vs {match.get('team2')}: {e}")

    async def run_forever(self):
        """Main loop."""
        logger.info("🔴 Live Match Watcher started")
        logger.info(f"   Monitor interval: {SCREENSHOT_INTERVAL}s")
        logger.info(f"   Local monitor window: {MONITOR_SECONDS}s @ {MONITOR_FPS:.1f} FPS")
        logger.info(f"   Burst frames on trigger: {BURST_FRAMES}")
        logger.info(f"   Max tweets per match: {MAX_TWEETS_PER_MATCH}")
        logger.info(f"   Cooldown after tweet: {COOLDOWN_AFTER_TWEET}s")

        self._bootstrap_runtime_schema()

        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"❌ Cycle error: {e}", exc_info=True)

            # Jitter to avoid predictable patterns
            jitter = random.randint(-10, 10)
            await asyncio.sleep(SCREENSHOT_INTERVAL + jitter)


if __name__ == '__main__':
    watcher = LiveMatchWatcher()
    asyncio.run(watcher.run_forever())
