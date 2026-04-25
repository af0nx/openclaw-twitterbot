#!/usr/bin/env python3
"""
Twitter Poster - Twitter Bot Pipeline V2
Executes tweets via X API v2 with strict 100/day quota enforcement (Pay Per Use tier)
Handles single tweets, replies, quote tweets, and threads
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
import os

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
import tweepy

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.media_manager import get_media_manager
from processing.tweet_quality import normalize_generated_text, tweet_quality_issue
from utils.account_quota import free_account_slot, get_account_quota_snapshot, increment_account_quota
from utils.db_utils import ensure_db_connection
from utils.runtime_schema import ensure_runtime_schema_extensions
from utils.twitter_accounts import ACCOUNT_BUCKETS, get_account_credentials, get_bucket_daily_cap

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MAX_QUEUED_TWEET_AGE_HOURS = int(os.getenv('MAX_QUEUED_TWEET_AGE_HOURS', '24'))


class TwitterPoster:
    """X API v2 posting with quota enforcement"""
    
    def __init__(self):
        self.db_conn = None
        self.client = None
        self.clients = {}
        self.media_manager = get_media_manager()
        self._schema_ready = False
        self.dry_run = os.getenv('DRY_RUN_MODE', 'false').lower() == 'true'
        self.review_only = os.getenv('DASHBOARD_REVIEW_ONLY', 'false').lower() in ('1', 'true', 'yes', 'on')
        
        # Initialize Twitter API v2 client
        self.init_twitter_clients()
        
    def init_twitter_clients(self):
        """Initialize Tweepy v2 clients for each account bucket."""
        self.clients = {}
        last_error = None

        for bucket in ACCOUNT_BUCKETS:
            try:
                creds = get_account_credentials(bucket)
                if not all([creds['api_key'], creds['api_secret'], creds['access_token'], creds['access_secret']]):
                    continue

                self.clients[bucket] = tweepy.Client(
                    bearer_token=creds['bearer_token'] or None,
                    consumer_key=creds['api_key'],
                    consumer_secret=creds['api_secret'],
                    access_token=creds['access_token'],
                    access_token_secret=creds['access_secret'],
                    wait_on_rate_limit=True,
                )
                logger.info(f"✅ Twitter API v2 client initialized for {bucket}")
            except Exception as e:
                last_error = e
                logger.error(f"❌ Failed to initialize Twitter client for {bucket}: {e}")

        self.client = self.clients.get('main') or next(iter(self.clients.values()), None)
        if not self.client:
            raise ValueError(f"No X clients available: {last_error or 'missing credentials'}")

    def get_client(self, bucket: str):
        return self.clients.get(bucket) or self.clients.get('main') or self.client
    
    def connect_db(self):
        """Establish PostgreSQL connection with auto-reconnect"""
        try:
            self.db_conn = ensure_db_connection(self.db_conn)
            if not self._schema_ready:
                ensure_runtime_schema_extensions(self.db_conn)
                self._schema_ready = True
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise

    def _ensure_db(self):
        """Lightweight reconnect guard"""
        self.db_conn = ensure_db_connection(self.db_conn)
    
    def increment_quota(self, bucket: str):
        """Increment executed writes counter for the posting bucket."""
        try:
            increment_account_quota(self.db_conn, bucket)
            logger.info(f"📊 Incremented quota counter for {bucket}")
        except Exception as e:
            logger.error(f"❌ Failed to increment quota for {bucket}: {e}")

    def expire_stale_queued_tweets(self):
        """Prevent old queue items from leaking into live posting."""
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE twitter_bot.tweets_v2
                    SET status = 'expired',
                        updated_at = NOW(),
                        posting_error = COALESCE(posting_error, 'expired by poster stale queue guard')
                    WHERE status = 'queued'
                      AND created_at < NOW() - (%s * INTERVAL '1 hour')
                    RETURNING COALESCE(account_bucket, 'main')
                    """,
                    (MAX_QUEUED_TWEET_AGE_HOURS,),
                )
                expired_buckets = [row[0] for row in cur.fetchall()]
                self.db_conn.commit()

            for bucket in expired_buckets:
                free_account_slot(self.db_conn, bucket)

            if expired_buckets:
                logger.warning(
                    "🗑️  Expired %s stale queued tweet(s) older than %sh",
                    len(expired_buckets),
                    MAX_QUEUED_TWEET_AGE_HOURS,
                )
        except Exception as e:
            logger.warning(f"⚠️  Failed to expire stale queued tweets: {e}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass

    def reject_unpostable_tweet(self, tweet_data: Dict[str, Any], reason: str):
        """Fail closed if a queued tweet contains prompt leakage or unsafe formatting."""
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET status = 'rejected',
                        posting_error = %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (f"poster preflight rejected: {reason}", tweet_data['id']))
                self.db_conn.commit()
            free_account_slot(self.db_conn, tweet_data.get('account_bucket') or 'main')
            logger.warning("🚫 Rejected queued tweet %s before posting: %s", tweet_data['id'], reason)
        except Exception as e:
            logger.error(f"❌ Failed to reject unpostable tweet {tweet_data.get('id')}: {e}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass

    def preflight_tweet_content(self, tweet_data: Dict[str, Any]) -> Optional[str]:
        content = normalize_generated_text(tweet_data.get('content') or '')
        issue = tweet_quality_issue(content)
        if issue:
            return issue

        if tweet_data.get('is_thread') and tweet_data.get('thread_tweets'):
            for index, thread_tweet in enumerate(tweet_data['thread_tweets'], start=1):
                thread_content = normalize_generated_text(thread_tweet)
                issue = tweet_quality_issue(thread_content)
                if issue:
                    return f"thread tweet {index}: {issue}"

        return None
    
    def post_single_tweet(self, content: str, media_ids: list = None, bucket: str = 'main') -> Optional[str]:
        """
        Post a single tweet, optionally with media
        
        Returns:
            Tweet ID if successful, None otherwise
        Raises:
            tweepy.TweepyException: re-raised for 402/429 so caller can pause
        """
        if self.dry_run:
            logger.info(f"🧪 DRY RUN: Would post: {content[:80]}...")
            return "dry_run_tweet_id"
        
        try:
            client = self.get_client(bucket)
            kwargs = {'text': content}
            if media_ids:
                kwargs['media_ids'] = media_ids
            response = client.create_tweet(**kwargs)
            tweet_id = response.data['id']
            
            logger.info(f"✅ Posted tweet: {tweet_id}")
            return tweet_id
            
        except tweepy.TweepyException as e:
            err_str = str(e)
            # Re-raise billing/rate/outage errors so the cycle can pause
            if any(code in err_str for code in ('402', '429', '503', 'Payment Required', 'Service Unavailable')):
                raise
            logger.error(f"❌ Twitter API error: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Failed to post tweet: {e}")
            return None
    
    def post_reply(self, content: str, reply_to_id: str, media_ids: list = None, bucket: str = 'replies') -> Optional[str]:
        """Post a reply to another tweet. Falls back to standalone if 403."""
        if self.dry_run:
            logger.info(f"🧪 DRY RUN: Would reply to {reply_to_id}: {content[:80]}...")
            return "dry_run_reply_id"
        
        try:
            client = self.get_client(bucket)
            kwargs = {'text': content, 'in_reply_to_tweet_id': reply_to_id}
            if media_ids:
                kwargs['media_ids'] = media_ids
            response = client.create_tweet(**kwargs)
            tweet_id = response.data['id']
            
            logger.info(f"✅ Posted reply: {tweet_id} → {reply_to_id}")
            return tweet_id
            
        except tweepy.TweepyException as e:
            err_str = str(e)
            # Re-raise billing/rate/outage errors so the cycle can pause
            if any(code in err_str for code in ('402', '429', '503', 'Payment Required', 'Service Unavailable')):
                raise
            if '403' in err_str:
                logger.warning(f"⚠️  Reply 403'd ({err_str[:80]}) — falling back to quote tweet")
                return self.post_quote_tweet(content, reply_to_id, media_ids, bucket=bucket)
            logger.error(f"❌ Twitter API error: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Failed to post reply: {e}")
            return None
    
    def post_quote_tweet(self, content: str, quote_tweet_id: str, media_ids: list = None, bucket: str = 'replies') -> Optional[str]:
        """Post a quote tweet. Falls back to standalone if 403."""
        if self.dry_run:
            logger.info(f"🧪 DRY RUN: Would quote {quote_tweet_id}: {content[:80]}...")
            return "dry_run_quote_id"
        
        try:
            client = self.get_client(bucket)
            kwargs = {'text': content, 'quote_tweet_id': quote_tweet_id}
            if media_ids:
                kwargs['media_ids'] = media_ids
            response = client.create_tweet(**kwargs)
            tweet_id = response.data['id']
            
            logger.info(f"✅ Posted quote tweet: {tweet_id}")
            return tweet_id
            
        except tweepy.TweepyException as e:
            err_str = str(e)
            # Re-raise billing/rate/outage errors so the cycle can pause
            if any(code in err_str for code in ('402', '429', '503', 'Payment Required', 'Service Unavailable')):
                raise
            if '403' in err_str:
                logger.warning(f"⚠️  Quote 403'd — falling back to standalone tweet")
                return self.post_single_tweet(content, media_ids, bucket=bucket)
            logger.error(f"❌ Twitter API error: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Failed to post quote tweet: {e}")
            return None
    
    def post_thread(self, tweets: List[str], media_ids: Optional[List[str]] = None, bucket: str = 'main') -> Optional[List[str]]:
        """
        Post a thread of tweets. Attaches media to the first tweet if provided.
        Each tweet in a thread counts as one API call toward the daily quota.
        
        Returns:
            List of tweet IDs if successful (may be partial on mid-thread failure)
        """
        if not tweets:
            logger.error("❌ Empty thread — nothing to post")
            return None
        
        if len(tweets) > 25:
            logger.error(f"❌ Thread too long ({len(tweets)} tweets, max 25)")
            return None
        
        if self.dry_run:
            logger.info(f"🧪 DRY RUN: Would post thread with {len(tweets)} tweets")
            return [f"dry_run_thread_{i}" for i in range(len(tweets))]
        
        client = self.get_client(bucket)
        tweet_ids = []
        previous_id = None
        
        for i, content in enumerate(tweets):
            try:
                kwargs = {'text': content}
                if previous_id:
                    kwargs['in_reply_to_tweet_id'] = previous_id
                # Attach media to first tweet only
                if i == 0 and media_ids:
                    kwargs['media_ids'] = media_ids
                response = client.create_tweet(**kwargs)
                
                tweet_id = response.data['id']
                tweet_ids.append(tweet_id)
                previous_id = tweet_id
                
                logger.info(f"✅ Posted thread tweet {i+1}/{len(tweets)}: {tweet_id}")
                
                # Count each tweet in the thread toward the daily quota
                if i > 0:
                    self.increment_quota(bucket)
                
                # Rate limiting between thread tweets
                if i < len(tweets) - 1:
                    time.sleep(2)
                    
            except tweepy.TweepyException as e:
                err_str = str(e)
                if any(code in err_str for code in ('402', '429', '503', 'Payment Required', 'Service Unavailable')):
                    logger.error(f"❌ Thread interrupted at tweet {i+1}/{len(tweets)} (billing/rate limit): {err_str[:100]}")
                    if tweet_ids:
                        logger.warning(f"⚠️  Partial thread posted: {len(tweet_ids)}/{len(tweets)} tweets")
                        return tweet_ids
                    raise
                logger.error(f"❌ Thread tweet {i+1}/{len(tweets)} failed: {e}")
                if tweet_ids:
                    logger.warning(f"⚠️  Partial thread posted: {len(tweet_ids)}/{len(tweets)} tweets")
                    return tweet_ids
                return None
            except Exception as e:
                logger.error(f"❌ Unexpected error on thread tweet {i+1}/{len(tweets)}: {e}")
                if tweet_ids:
                    logger.warning(f"⚠️  Partial thread posted: {len(tweet_ids)}/{len(tweets)} tweets")
                    return tweet_ids
                return None
        
        logger.info(f"✅ Posted complete thread: {len(tweet_ids)} tweets")
        return tweet_ids
    
    async def process_queued_tweet(self, tweet_id: str) -> bool:
        """
        Process and post a single queued tweet
        
        Returns:
            True if successfully posted
        """
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                      SELECT t.id, t.content, t.reply_target_id, t.quote_tweet_id, t.pillar, t.event_id,
                          e.metadata, t.media_path, t.is_thread, t.thread_tweets, t.scheduled_post_at,
                          COALESCE(t.account_bucket, 'main')
                    FROM twitter_bot.tweets_v2 t
                    LEFT JOIN twitter_bot.events e ON t.event_id = e.id
                    WHERE t.id = %s AND t.status = 'queued'
                """, (tweet_id,))
                
                row = cur.fetchone()
                if not row:
                    logger.warning(f"⚠️  Tweet {tweet_id} not found or not queued")
                    return False
                
                tweet_data = {
                    'id': str(row[0]),
                    'content': row[1],
                    'reply_target_id': row[2],
                    'quote_tweet_id': row[3],
                    'pillar': row[4],
                    'event_id': str(row[5]) if row[5] else None,
                    'metadata': row[6] or {},
                    'media_path': row[7],
                    'is_thread': row[8] or False,
                    'thread_tweets': row[9],
                    'scheduled_post_at': row[10],
                    'account_bucket': row[11] or 'main',
                }
            
            # Check if this tweet is scheduled for later
            if tweet_data['scheduled_post_at']:
                now = datetime.now(timezone.utc)
                if tweet_data['scheduled_post_at'] > now:
                    logger.info(f"⏰ Tweet {tweet_id} scheduled for {tweet_data['scheduled_post_at']}, skipping")
                    return False

            preflight_issue = self.preflight_tweet_content(tweet_data)
            if preflight_issue:
                self.reject_unpostable_tweet(tweet_data, preflight_issue)
                return False

            if self.dry_run:
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        UPDATE twitter_bot.tweets_v2
                        SET status = 'dry_run',
                            posting_error = 'dry run simulated; no X API write executed',
                            updated_at = NOW()
                        WHERE id = %s
                    """, (tweet_id,))
                    self.db_conn.commit()
                free_account_slot(self.db_conn, tweet_data['account_bucket'])
                logger.info("🧪 DRY RUN: validated tweet %s without posting: %s", tweet_id, tweet_data['content'][:100])
                return True
            
            # Build media_ids list if media is attached
            media_ids = None
            media_ref = tweet_data['media_path']
            if media_ref:
                if os.path.exists(str(media_ref)):
                    uploaded = self.media_manager.upload_media(str(media_ref), account_bucket=tweet_data['account_bucket'])
                    if uploaded:
                        media_ids = [uploaded]
                else:
                    media_ids = [media_ref]
            
            # Determine tweet type and post
            posted_id = None
            
            if tweet_data['is_thread'] and tweet_data['thread_tweets']:
                # Post as a thread (attach media to first tweet)
                thread_ids = self.post_thread(
                    tweet_data['thread_tweets'],
                    media_ids=media_ids,
                    bucket=tweet_data['account_bucket'],
                )
                if thread_ids:
                    posted_id = thread_ids[0]  # First tweet is the "main" one
                    logger.info(f"🧵 Thread posted: {len(thread_ids)} tweets")
            elif tweet_data['reply_target_id']:
                # VIP/engagement replies always 403 on Free tier — go straight to quote tweet
                if tweet_data['pillar'] in (12, 16):
                    posted_id = self.post_quote_tweet(
                        tweet_data['content'],
                        tweet_data['reply_target_id'],
                        media_ids=media_ids,
                        bucket=tweet_data['account_bucket'],
                    )
                else:
                    posted_id = self.post_reply(
                        tweet_data['content'],
                        tweet_data['reply_target_id'],
                        media_ids=media_ids,
                        bucket=tweet_data['account_bucket'],
                    )
            elif tweet_data['quote_tweet_id']:
                posted_id = self.post_quote_tweet(
                    tweet_data['content'],
                    tweet_data['quote_tweet_id'],
                    media_ids=media_ids,
                    bucket=tweet_data['account_bucket'],
                )
            else:
                posted_id = self.post_single_tweet(
                    tweet_data['content'],
                    media_ids=media_ids,
                    bucket=tweet_data['account_bucket'],
                )
            
            if not posted_id:
                logger.error(f"❌ Failed to post tweet {tweet_id}")
                
                # Mark as retry (up to 3 attempts) instead of permanent failure
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        SELECT posting_error
                        FROM twitter_bot.tweets_v2 WHERE id = %s
                    """, (tweet_id,))
                    row = cur.fetchone()
                    prev_error = row[0] if row else ''
                    
                    # Parse retry count from posting_error field (e.g. "Retry 2/3")
                    retry_count = 0
                    if prev_error and prev_error.startswith('Retry '):
                        try:
                            retry_count = int(prev_error.split('/')[0].split(' ')[1])
                        except (ValueError, IndexError):
                            pass
                    
                    if retry_count >= 3:
                        cur.execute("""
                            UPDATE twitter_bot.tweets_v2
                            SET status = 'failed',
                                posting_error = 'Max retries (3) exhausted',
                                updated_at = NOW()
                            WHERE id = %s
                        """, (tweet_id,))
                        free_account_slot(self.db_conn, tweet_data['account_bucket'])
                        logger.error(f"💀 Tweet {tweet_id} permanently failed after 3 retries")
                    else:
                        # Keep as queued but bump retry count — will be picked up next cycle
                        cur.execute("""
                            UPDATE twitter_bot.tweets_v2
                            SET posting_error = %s,
                                updated_at = NOW()
                            WHERE id = %s
                        """, (f"Retry {retry_count + 1}/3", tweet_id))
                        logger.warning(f"🔄 Tweet {tweet_id} retry {retry_count + 1}/3 scheduled")
                    self.db_conn.commit()
                
                return False
            
            # Update tweet record
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET status = 'posted',
                        twitter_tweet_id = %s,
                        posted_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                """, (posted_id, tweet_id))
                
                # Track posted VIP engagement regardless of whether we quoted or replied.
                if tweet_data['pillar'] == 12 and (tweet_data['reply_target_id'] or tweet_data['quote_tweet_id']):
                    vip_username = tweet_data['metadata'].get('vip_username', 'unknown')
                    cur.execute("""
                        INSERT INTO twitter_bot.hitl_engaged_7d
                        (twitter_username, our_reply_tweet_id, engaged_at)
                        VALUES (%s, %s, NOW())
                    """, (vip_username, posted_id))
                
                self.db_conn.commit()
            
            # Increment quota
            self.increment_quota(tweet_data['account_bucket'])
            
            logger.info(f"✅ Successfully posted tweet {tweet_id} → {posted_id}")
            return True
            
        except tweepy.TweepyException as e:
            err_str = str(e)
            if any(code in err_str for code in ('402', '429', '503', 'Payment Required', 'Service Unavailable')):
                logger.error(f"🚫 Rate limit / no credits / API outage for tweet {tweet_id}: {err_str[:120]}")
                # Re-raise so run_cycle stops trying more tweets this cycle
                raise
            logger.error(f"❌ Twitter API error processing tweet {tweet_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Failed to process tweet {tweet_id}: {e}")
            return False
    
    async def run_cycle(self):
        """Process queued tweets"""
        try:
            self._ensure_db()
            # Rollback any open implicit transaction so CURRENT_DATE is fresh
            self.db_conn.rollback()
            self.expire_stale_queued_tweets()

            if self.review_only:
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        SELECT COUNT(*)
                        FROM twitter_bot.tweets_v2
                        WHERE status = 'queued'
                    """)
                    queued_count = int(cur.fetchone()[0] or 0)
                if queued_count:
                    logger.info(
                        "🧾 Dashboard review-only mode active — holding %s queued tweet(s) for manual review",
                        queued_count,
                    )
                else:
                    logger.debug("🧾 Dashboard review-only mode active — no queued tweets to hold")
                return

            queued_tweets = []

            active_buckets = []
            for bucket in ACCOUNT_BUCKETS:
                snapshot = get_account_quota_snapshot(self.db_conn, bucket)
                daily_cap = get_bucket_daily_cap(bucket)
                if snapshot['hard_capped'] and snapshot['writes_reserved'] == 0:
                    continue
                if snapshot['writes_executed'] < daily_cap or snapshot['writes_reserved'] > 0:
                    active_buckets.append(bucket)

            if not active_buckets:
                logger.warning("🚫 All account buckets are quota-capped")
                return

            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT t.id
                    FROM twitter_bot.tweets_v2 t
                    LEFT JOIN twitter_bot.events e ON t.event_id = e.id
                    WHERE t.status = 'queued'
                      AND (t.scheduled_post_at IS NULL OR t.scheduled_post_at <= NOW())
                      AND t.created_at >= NOW() - (%s * INTERVAL '1 hour')
                      AND COALESCE(t.account_bucket, 'main') = ANY(%s)
                    ORDER BY 
                        CASE t.pillar
                            WHEN 1 THEN 1
                            WHEN 2 THEN 2
                            ELSE 3
                        END,
                        t.created_at ASC
                    LIMIT 10
                """, (MAX_QUEUED_TWEET_AGE_HOURS, active_buckets,))

                queued_tweets = [str(row[0]) for row in cur.fetchall()]
            
            if not queued_tweets:
                logger.debug("ℹ️  No queued tweets")
                return
            
            logger.info(f"🔄 Processing {len(queued_tweets)} queued tweets...")
            
            for tweet_id in queued_tweets:
                try:
                    success = await self.process_queued_tweet(tweet_id)
                except tweepy.TweepyException as e:
                    err_str = str(e)
                    if any(code in err_str for code in ('402', '429', '503', 'Payment Required', 'Service Unavailable')):
                        logger.warning("💳 No credits / rate limited / API outage — pausing poster for this cycle. Tweets stay queued.")
                        return  # Stop processing, tweets remain queued for next cycle
                    raise
                
                # Rate limiting between posts
                await asyncio.sleep(3)
            
        except Exception as e:
            logger.error(f"❌ Cycle failed: {e}")
        finally:
            # Always close implicit transaction to prevent stale CURRENT_DATE
            try:
                self.db_conn.rollback()
            except Exception:
                pass
    
    async def run_forever(self):
        """Main loop - runs every 2 minutes"""
        self.connect_db()
        logger.info("🚀 Twitter Poster started")
        
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"❌ Run cycle failed: {e}")
            
            # Run every minute
            await asyncio.sleep(60)
    
    def cleanup(self):
        """Cleanup resources"""
        if self.db_conn:
            self.db_conn.close()


async def main():
    poster = TwitterPoster()
    try:
        await poster.run_forever()
    except KeyboardInterrupt:
        logger.info("⏹️  Shutting down Twitter Poster...")
    finally:
        poster.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
