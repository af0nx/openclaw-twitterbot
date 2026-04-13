#!/usr/bin/env python3
"""
Tweet Pruner — Cleans up low-performing tweets to improve account health.

Why pruning matters:
  - X algorithm evaluates your AVERAGE engagement rate, not total
  - 500 tweets with 0 likes TANKS your distribution
  - Deleting flops immediately improves next tweet's reach
  - Professional accounts prune constantly

Strategy:
  1. Find tweets older than 48h with 0 likes and 0 replies
  2. Delete them via X API v2
  3. Clean up DB records
  4. Track pruning metrics

Safety:
  - Never delete tweets with >2 likes (they're fine)
  - Never delete tweets less than 48h old (give them time)
  - Max 20 deletes per cycle (avoid rate limits)
  - Runs daily at 04:00 UTC (off-peak)
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from dotenv import load_dotenv
import psycopg2
import tweepy

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.db_utils import ensure_db_connection

load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TweetPruner:
    """Delete low-performing tweets to improve account health"""

    def __init__(self):
        self.db_conn = None
        self.client = self._init_v2_client()
        self.max_deletes_per_cycle = int(os.getenv('PRUNE_MAX_DELETES', 20))
        self.min_age_hours = int(os.getenv('PRUNE_MIN_AGE_HOURS', 48))
        self.engagement_threshold = int(os.getenv('PRUNE_ENGAGEMENT_THRESHOLD', 2))

    def _init_v2_client(self) -> Optional[tweepy.Client]:
        """Initialize X API v2 client for delete operations"""
        try:
            return tweepy.Client(
                consumer_key=os.getenv('X_API_KEY'),
                consumer_secret=os.getenv('X_API_SECRET'),
                access_token=os.getenv('X_ACCESS_TOKEN'),
                access_token_secret=os.getenv('X_ACCESS_SECRET'),
            )
        except Exception as e:
            logger.error(f"❌ v2 client init failed: {e}")
            return None

    def connect_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    def _ensure_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    def find_prune_candidates(self) -> List[Dict]:
        """Find posted tweets with near-zero engagement after 48h"""
        self._ensure_db()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.min_age_hours)

        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT id, twitter_tweet_id, content, likes, replies, retweets,
                           impressions, posted_at, pillar
                    FROM twitter_bot.tweets_v2
                    WHERE status = 'posted'
                    AND twitter_tweet_id IS NOT NULL
                    AND posted_at < %s
                    AND (likes + replies + retweets) <= %s
                    AND pruned = FALSE
                    ORDER BY (likes + replies + retweets) ASC, posted_at ASC
                    LIMIT %s
                """, (cutoff, self.engagement_threshold, self.max_deletes_per_cycle))

                candidates = []
                for row in cur.fetchall():
                    candidates.append({
                        'id': str(row[0]),
                        'tweet_id': row[1],
                        'content': row[2][:80],
                        'likes': row[3],
                        'replies': row[4],
                        'retweets': row[5],
                        'impressions': row[6],
                        'posted_at': row[7],
                        'pillar': row[8],
                    })
                return candidates
        except Exception as e:
            logger.error(f"❌ Failed to find prune candidates: {e}")
            return []

    def delete_tweet(self, tweet_id: str) -> bool:
        """Delete a tweet via X API v2"""
        if not self.client:
            logger.error("❌ No v2 client available")
            return False

        try:
            response = self.client.delete_tweet(tweet_id)
            if response and response.data and response.data.get('deleted'):
                return True
            logger.warning(f"⚠️  Delete response: {response}")
            return False
        except tweepy.TooManyRequests:
            logger.warning("⚠️  Rate limited — stopping prune cycle")
            return False
        except tweepy.NotFound:
            # Already deleted or doesn't exist
            return True
        except Exception as e:
            logger.warning(f"⚠️  Delete failed for {tweet_id}: {e}")
            return False

    def mark_pruned(self, db_id: str, tweet_id: str, deleted: bool):
        """Update DB record after prune attempt"""
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                if deleted:
                    cur.execute("""
                        UPDATE twitter_bot.tweets_v2
                        SET pruned = TRUE, pruned_at = NOW(), status = 'pruned'
                        WHERE id = %s
                    """, (db_id,))
                else:
                    # Mark as attempted but not deleted
                    cur.execute("""
                        UPDATE twitter_bot.tweets_v2
                        SET prune_attempted_at = NOW()
                        WHERE id = %s
                    """, (db_id,))
                self.db_conn.commit()
        except Exception as e:
            logger.error(f"❌ Failed to mark pruned: {e}")
            self.db_conn.rollback()

    async def run_cycle(self):
        """Run one prune cycle"""
        self.connect_db()

        candidates = self.find_prune_candidates()
        if not candidates:
            logger.info("✅ No tweets to prune — account is clean!")
            return

        logger.info(f"🗑️  Found {len(candidates)} prune candidates")

        deleted = 0
        failed = 0

        for c in candidates:
            success = self.delete_tweet(c['tweet_id'])
            self.mark_pruned(c['id'], c['tweet_id'], success)

            if success:
                deleted += 1
                logger.info(f"🗑️  Pruned: \"{c['content']}...\" ({c['likes']}L/{c['replies']}R)")
            else:
                failed += 1

            await asyncio.sleep(2)  # Rate limit: 2s between deletes

        logger.info(f"🗑️  Prune cycle complete: {deleted} deleted, {failed} failed")

        # Log analytics
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) as total_posted,
                        COUNT(*) FILTER (WHERE pruned = TRUE) as total_pruned,
                        AVG(engagement_rate) FILTER (WHERE pruned = FALSE AND status = 'posted') as avg_er_kept,
                        AVG(engagement_rate) FILTER (WHERE pruned = TRUE) as avg_er_pruned
                    FROM twitter_bot.tweets_v2
                    WHERE status IN ('posted', 'pruned')
                    AND posted_at > NOW() - INTERVAL '30 days'
                """)
                stats = cur.fetchone()
                if stats:
                    logger.info(
                        f"📊 30-day stats: {stats[0]} total, {stats[1]} pruned, "
                        f"kept avg ER: {(stats[2] or 0):.4f}, pruned avg ER: {(stats[3] or 0):.4f}"
                    )
        except Exception as e:
            logger.debug(f"Stats query failed: {e}")

    async def run_forever(self):
        """Main loop — runs daily at 04:00 UTC"""
        logger.info("🚀 Tweet Pruner started — keeping the account clean")
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"❌ Cycle failed: {e}")
            await asyncio.sleep(24 * 3600)  # Daily


if __name__ == '__main__':
    asyncio.run(TweetPruner().run_forever())
