#!/usr/bin/env python3
"""
Analytics Tracker - Twitter Bot Pipeline V2
Pulls engagement metrics from X API every 6 hours
Updates PostgreSQL with impressions, likes, replies for RLHF feedback loop
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import os

from dotenv import load_dotenv
import psycopg2
import tweepy

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class AnalyticsTracker:
    """Track tweet engagement for RLHF training"""
    
    def __init__(self):
        self.db_conn = None
        self.client = None
        
    def connect_db(self):
        """Establish PostgreSQL connection"""
        try:
            self.db_conn = psycopg2.connect(os.getenv('DATABASE_URL'))
            logger.info("✅ Connected to PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise
    
    def init_twitter_client(self):
        """Initialize Tweepy v2 client (read-only)"""
        try:
            bearer_token = os.getenv('X_BEARER_TOKEN')
            
            if not bearer_token:
                raise ValueError("X_BEARER_TOKEN not set")
            
            self.client = tweepy.Client(
                bearer_token=bearer_token,
                wait_on_rate_limit=True
            )
            
            logger.info("✅ Twitter API v2 client initialized")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Twitter client: {e}")
            raise
    
    def fetch_tweets_needing_metrics(self, hours: int = 7 * 24) -> List[Dict[str, Any]]:
        """
        Fetch tweets that need metrics update
        
        Args:
            hours: Look back this many hours
        
        Returns:
            List of tweet records
        """
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT id, twitter_tweet_id, posted_at, content
                    FROM twitter_bot.tweets_v2
                    WHERE status = 'posted'
                    AND twitter_tweet_id IS NOT NULL
                    AND posted_at > NOW() - (%s || ' hours')::interval
                    AND (
                        metrics_last_updated IS NULL
                        OR metrics_last_updated < NOW() - INTERVAL '6 hours'
                    )
                    ORDER BY posted_at DESC
                    LIMIT 100
                """, (hours,))
                
                tweets = []
                for row in cur.fetchall():
                    tweets.append({
                        'id': str(row[0]),
                        'twitter_tweet_id': row[1],
                        'posted_at': row[2],
                        'content': row[3]
                    })
                
                logger.info(f"📊 Found {len(tweets)} tweets needing metrics update")
                return tweets
                
        except Exception as e:
            logger.error(f"❌ Failed to fetch tweets: {e}")
            return []
    
    def fetch_tweet_metrics(self, tweet_id: str) -> Optional[Dict[str, int]]:
        """
        Fetch engagement metrics for a single tweet via X API v2
        
        Args:
            tweet_id: Twitter tweet ID
        
        Returns:
            Dict with metrics or None
        """
        try:
            # X API v2 tweet lookup with metrics
            tweet = self.client.get_tweet(
                tweet_id,
                tweet_fields=['public_metrics'],
                user_auth=False  # Bearer token only
            )
            
            if not tweet.data:
                logger.warning(f"⚠️  Tweet {tweet_id} not found")
                return None
            
            metrics = tweet.data.public_metrics
            
            return {
                'impressions': metrics.get('impression_count', 0),
                'likes': metrics.get('like_count', 0),
                'replies': metrics.get('reply_count', 0),
                'retweets': metrics.get('retweet_count', 0),
                'quotes': metrics.get('quote_count', 0)
            }
            
        except tweepy.TweepyException as e:
            logger.error(f"❌ Twitter API error for {tweet_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Failed to fetch metrics for {tweet_id}: {e}")
            return None
    
    def update_tweet_metrics(self, internal_id: str, metrics: Dict[str, int]):
        """Update tweet record with fresh metrics"""
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET impressions = %s,
                        likes = %s,
                        replies = %s,
                        retweets = %s,
                        quote_tweets = %s,
                        engagement_rate = CASE 
                            WHEN %s > 0 THEN 
                                ((%s::float + %s + %s + %s) / %s::float) * 100
                            ELSE 0
                        END,
                        metrics_last_updated = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                """, (
                    metrics['impressions'],
                    metrics['likes'],
                    metrics['replies'],
                    metrics['retweets'],
                    metrics['quotes'],
                    metrics['impressions'],  # For engagement_rate calc
                    metrics['likes'],
                    metrics['replies'],
                    metrics['retweets'],
                    metrics['quotes'],
                    metrics['impressions'],
                    internal_id
                ))
                
                self.db_conn.commit()
                
                logger.debug(f"✅ Updated metrics for {internal_id}")
                
        except Exception as e:
            logger.error(f"❌ Failed to update metrics: {e}")
    
    def classify_rlhf_performance(self):
        """
        Classify tweets as top_5, bottom_5, or neutral for RLHF training
        Runs weekly - classifies past 7 days
        """
        try:
            with self.db_conn.cursor() as cur:
                # Reset all to neutral first
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET rlhf_classified = 'neutral'
                    WHERE posted_at > NOW() - INTERVAL '7 days'
                    AND status = 'posted'
                """)
                
                # Mark top 5
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET rlhf_classified = 'top_5'
                    WHERE id IN (
                        SELECT id
                        FROM twitter_bot.tweets_v2
                        WHERE posted_at > NOW() - INTERVAL '7 days'
                        AND status = 'posted'
                        AND impressions > 0
                        ORDER BY engagement_rate DESC
                        LIMIT 5
                    )
                """)
                
                # Mark bottom 5
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET rlhf_classified = 'bottom_5'
                    WHERE id IN (
                        SELECT id
                        FROM twitter_bot.tweets_v2
                        WHERE posted_at > NOW() - INTERVAL '7 days'
                        AND status = 'posted'
                        AND impressions > 0
                        ORDER BY engagement_rate ASC
                        LIMIT 5
                    )
                """)
                
                self.db_conn.commit()
                
                logger.info("✅ Classified tweets for RLHF training")
                
        except Exception as e:
            logger.error(f"❌ Failed to classify RLHF performance: {e}")
    
    async def run_metrics_cycle(self):
        """Process metrics update for pending tweets"""
        try:
            # Fetch tweets needing updates
            tweets = self.fetch_tweets_needing_metrics()
            
            if not tweets:
                logger.debug("ℹ️  No tweets need metrics update")
                return
            
            logger.info(f"🔄 Updating metrics for {len(tweets)} tweets...")
            
            success_count = 0
            
            for tweet in tweets:
                metrics = self.fetch_tweet_metrics(tweet['twitter_tweet_id'])
                
                if metrics:
                    self.update_tweet_metrics(tweet['id'], metrics)
                    success_count += 1
                
                # Rate limiting
                await asyncio.sleep(2)
            
            logger.info(f"✅ Updated {success_count}/{len(tweets)} tweets")
            
            # Run RLHF classification (only on Sundays)
            if datetime.now(timezone.utc).weekday() == 6:  # Sunday
                logger.info("📊 Running weekly RLHF classification...")
                self.classify_rlhf_performance()
            
        except Exception as e:
            logger.error(f"❌ Metrics cycle failed: {e}")
    
    async def run_forever(self):
        """Main loop - runs every 6 hours"""
        self.connect_db()
        self.init_twitter_client()
        
        logger.info("🚀 Analytics Tracker started (6h cycle)")
        
        while True:
            try:
                await self.run_metrics_cycle()
            except Exception as e:
                logger.error(f"❌ Cycle failed: {e}")
            
            # Run every 6 hours
            await asyncio.sleep(6 * 60 * 60)
    
    def cleanup(self):
        """Cleanup resources"""
        if self.db_conn:
            self.db_conn.close()


async def main():
    tracker = AnalyticsTracker()
    try:
        await tracker.run_forever()
    except KeyboardInterrupt:
        logger.info("⏹️  Shutting down Analytics Tracker...")
    finally:
        tracker.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
