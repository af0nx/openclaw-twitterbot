#!/usr/bin/env python3
"""
Follower Growth Tracker - Twitter Bot Pipeline V2 Growth Engine
Tracks daily follower count, identifies new followers, and monitors
growth metrics to optimize content strategy.

Runs as PM2 daemon, checks every 60 minutes.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
import psycopg2
import tweepy

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.db_utils import ensure_db_connection

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class FollowerGrowthTracker:
    """Track follower growth and identify growth patterns"""
    
    def __init__(self):
        self.db_conn = None
        self.api = None
        self.our_user_id = os.getenv('X_USER_ID')
        
    def connect_db(self):
        """Establish PostgreSQL connection with auto-reconnect"""
        try:
            self.db_conn = ensure_db_connection(self.db_conn, autocommit=True)
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise
    
    def init_twitter_client(self):
        """Initialize Tweepy v1.1 API client (for verify_credentials). Reuses existing client."""
        if self.api is not None:
            return
        try:
            auth = tweepy.OAuth1UserHandler(
                os.getenv('X_API_KEY'),
                os.getenv('X_API_SECRET'),
                os.getenv('X_ACCESS_TOKEN'),
                os.getenv('X_ACCESS_SECRET')
            )
            self.api = tweepy.API(auth, wait_on_rate_limit=True)
            logger.info("✅ Twitter API v1.1 client initialized")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Twitter client: {e}")
            raise
    
    def ensure_tables(self):
        """Create growth tracking tables if they don't exist"""
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS twitter_bot.follower_snapshots (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        checked_at TIMESTAMPTZ DEFAULT now(),
                        follower_count INT NOT NULL,
                        following_count INT NOT NULL,
                        tweet_count INT NOT NULL,
                        listed_count INT DEFAULT 0,
                        daily_change INT DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS idx_follower_snapshots_checked
                        ON twitter_bot.follower_snapshots(checked_at DESC);
                """)
            logger.info("✅ Growth tracking tables ready")
        except Exception as e:
            logger.error(f"❌ Failed to create tables: {e}")
    
    def get_account_metrics(self) -> dict:
        """Fetch current account metrics from X API v1.1"""
        try:
            user = self.api.verify_credentials()
            
            if not user:
                logger.warning("⚠️  Could not fetch account metrics")
                return None
            
            return {
                'follower_count': user.followers_count,
                'following_count': user.friends_count,
                'tweet_count': user.statuses_count,
                'listed_count': getattr(user, 'listed_count', 0)
            }
        except Exception as e:
            logger.error(f"❌ Failed to fetch account metrics: {e}")
            return None
    
    def record_snapshot(self, metrics: dict):
        """Record a follower count snapshot"""
        try:
            with self.db_conn.cursor() as cur:
                # Get previous snapshot for daily_change calculation
                cur.execute("""
                    SELECT follower_count 
                    FROM twitter_bot.follower_snapshots
                    ORDER BY checked_at DESC
                    LIMIT 1
                """)
                prev = cur.fetchone()
                daily_change = metrics['follower_count'] - prev[0] if prev else 0
                
                cur.execute("""
                    INSERT INTO twitter_bot.follower_snapshots
                    (follower_count, following_count, tweet_count, listed_count, daily_change)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    metrics['follower_count'],
                    metrics['following_count'],
                    metrics['tweet_count'],
                    metrics['listed_count'],
                    daily_change
                ))
                
                logger.info(
                    f"📊 Snapshot: {metrics['follower_count']} followers "
                    f"({'+' if daily_change >= 0 else ''}{daily_change} since last check)"
                )
                
                # Log milestone alerts
                if metrics['follower_count'] >= 100 and prev and prev[0] < 100:
                    logger.info("🎉 MILESTONE: 100 followers reached!")
                elif metrics['follower_count'] >= 250 and prev and prev[0] < 250:
                    logger.info("🎉 MILESTONE: 250 followers reached!")
                elif metrics['follower_count'] >= 500 and prev and prev[0] < 500:
                    logger.info("🎉 MILESTONE: 500 followers reached!")
                elif metrics['follower_count'] >= 1000 and prev and prev[0] < 1000:
                    logger.info("🏆 TARGET REACHED: 1,000 followers!")
                    
        except Exception as e:
            logger.error(f"❌ Failed to record snapshot: {e}")
    
    def log_growth_summary(self):
        """Log a summary of recent growth"""
        try:
            with self.db_conn.cursor() as cur:
                # Last 24h growth
                cur.execute("""
                    SELECT 
                        MIN(follower_count) as min_followers,
                        MAX(follower_count) as max_followers,
                        MAX(follower_count) - MIN(follower_count) as growth_24h
                    FROM twitter_bot.follower_snapshots
                    WHERE checked_at > now() - INTERVAL '24 hours'
                """)
                row = cur.fetchone()
                if row and row[0]:
                    logger.info(f"📈 24h Growth: {row[2]:+d} followers ({row[0]} → {row[1]})")
                
                # Last 7d growth
                cur.execute("""
                    SELECT 
                        MIN(follower_count) as min_followers,
                        MAX(follower_count) as max_followers,
                        MAX(follower_count) - MIN(follower_count) as growth_7d
                    FROM twitter_bot.follower_snapshots
                    WHERE checked_at > now() - INTERVAL '7 days'
                """)
                row = cur.fetchone()
                if row and row[0]:
                    daily_avg = row[2] / 7.0
                    days_to_1000 = max(0, (1000 - row[1]) / daily_avg) if daily_avg > 0 else float('inf')
                    logger.info(
                        f"📈 7d Growth: {row[2]:+d} followers "
                        f"(avg {daily_avg:.1f}/day, est {days_to_1000:.0f} days to 1K)"
                    )
                    
        except Exception as e:
            logger.error(f"❌ Failed to log growth summary: {e}")
    
    async def run_cycle(self):
        """Single check cycle"""
        self.db_conn = ensure_db_connection(self.db_conn, autocommit=True)
        metrics = self.get_account_metrics()
        if metrics:
            self.record_snapshot(metrics)
            self.log_growth_summary()
    
    async def run_forever(self):
        """Main loop - check every 60 minutes"""
        self.connect_db()
        self.init_twitter_client()
        self.ensure_tables()
        
        logger.info("🚀 Follower Growth Tracker started (60min cycle)")
        
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"❌ Cycle failed: {e}")
            
            await asyncio.sleep(3600)  # 60 minutes
    
    def cleanup(self):
        if self.db_conn:
            self.db_conn.close()


async def main():
    tracker = FollowerGrowthTracker()
    try:
        await tracker.run_forever()
    except KeyboardInterrupt:
        logger.info("⏹️  Shutting down Follower Growth Tracker...")
    finally:
        tracker.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
