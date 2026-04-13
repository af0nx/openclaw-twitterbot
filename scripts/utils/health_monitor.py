#!/usr/bin/env python3
"""
Health Monitor - Twitter Bot Pipeline V2
Shadowban canary detection using different IP verification
Posts test tweets and verifies visibility from diverse geographic endpoints
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
import os
import random

from dotenv import load_dotenv
import psycopg2
import tweepy
import httpx

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class HealthMonitor:
    """Shadowban detection and account health monitoring"""
    
    def __init__(self):
        self.db_conn = None
        self.client = None
        
        # Canary tweet patterns (innocuous test content)
        self.canary_patterns = [
            "Market moving.",
            "Odds are moving.",
            "Numbers looking interesting.",
            "Charts tell the story.",
            "Data drop incoming.",
            "Live action heating up.",
            "Timeline is cooking.",
            "Something shifted.",
            "Watching this closely.",
            "Signal detected."
        ]
        
    def connect_db(self):
        """Establish PostgreSQL connection"""
        try:
            self.db_conn = psycopg2.connect(os.getenv('DATABASE_URL'))
            logger.info("✅ Connected to PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise
    
    def init_twitter_client(self):
        """Initialize Tweepy v2 client"""
        try:
            api_key = os.getenv('X_API_KEY')
            api_secret = os.getenv('X_API_SECRET')
            access_token = os.getenv('X_ACCESS_TOKEN')
            access_secret = os.getenv('X_ACCESS_SECRET')
            bearer_token = os.getenv('X_BEARER_TOKEN')
            
            if not all([api_key, api_secret, access_token, access_secret]):
                raise ValueError("Missing X API credentials")
            
            self.client = tweepy.Client(
                bearer_token=bearer_token,
                consumer_key=api_key,
                consumer_secret=api_secret,
                access_token=access_token,
                access_token_secret=access_secret,
                wait_on_rate_limit=True
            )
            
            logger.info("✅ Twitter API v2 client initialized")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Twitter client: {e}")
            raise
    
    def post_canary_tweet(self) -> Optional[str]:
        """
        Post an innocuous canary tweet
        
        Returns:
            Tweet ID if successful
        """
        try:
            content = random.choice(self.canary_patterns)
            
            response = self.client.create_tweet(text=content)
            tweet_id = response.data['id']
            
            logger.info(f"🐤 Posted canary tweet: {tweet_id}")
            return tweet_id
            
        except tweepy.TweepyException as e:
            logger.error(f"❌ Twitter API error: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Failed to post canary: {e}")
            return None
    
    async def verify_tweet_visibility(self, tweet_id: str, verification_ip: str) -> Dict[str, Any]:
        """
        Verify tweet is visible from a different IP
        
        Args:
            tweet_id: Tweet ID to verify
            verification_ip: IP address to verify from (proxy)
        
        Returns:
            Dict with verification results
        """
        try:
            # Use Twitter's oEmbed API (public, no auth required)
            # Can be called through a proxy to verify from different IP
            
            url = f"https://publish.twitter.com/oembed?url=https://twitter.com/user/status/{tweet_id}"
            
            async with httpx.AsyncClient(
                proxies=f"http://{verification_ip}" if verification_ip else None,
                timeout=30.0
            ) as client:
                response = await client.get(url)
            
            if response.status_code == 200:
                # Tweet is public and visible
                logger.info(f"✅ Canary {tweet_id} visible from {verification_ip or 'direct'}")
                return {
                    'visible': True,
                    'verification_ip': verification_ip,
                    'status_code': 200
                }
            elif response.status_code == 404:
                # Tweet not found - potential shadowban
                logger.warning(f"⚠️  Canary {tweet_id} NOT visible (404) from {verification_ip}")
                return {
                    'visible': False,
                    'verification_ip': verification_ip,
                    'status_code': 404
                }
            else:
                logger.warning(f"⚠️  Unexpected status {response.status_code} for {tweet_id}")
                return {
                    'visible': None,
                    'verification_ip': verification_ip,
                    'status_code': response.status_code
                }
            
        except Exception as e:
            logger.error(f"❌ Verification failed: {e}")
            return {
                'visible': None,
                'verification_ip': verification_ip,
                'error': str(e)
            }
    
    async def check_search_visibility(self, tweet_id: str) -> bool:
        """
        Check if tweet appears in Twitter search
        Uses authenticated search to verify
        
        Returns:
            True if found in search
        """
        try:
            # Search for the exact tweet
            # In production, search for unique phrase from canary
            
            # Note: This requires Twitter API v2 search endpoint
            # Basic tier may not have access - implement if available
            
            logger.info(f"🔍 Checking search visibility for {tweet_id}")
            
            # Placeholder - implement with actual search API
            return True
            
        except Exception as e:
            logger.error(f"❌ Search check failed: {e}")
            return False
    
    def log_canary_result(self, tweet_id: str, test_result: str, verification_ip: str):
        """Log canary test result to database"""
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO twitter_bot.canary_logs
                    (twitter_tweet_id, test_result, scrapling_verification_ip, timestamp)
                    VALUES (%s, %s, %s, NOW())
                """, (tweet_id, test_result, verification_ip))
                
                self.db_conn.commit()
                
                logger.debug(f"✅ Logged canary result: {test_result}")
                
        except Exception as e:
            logger.error(f"❌ Failed to log canary: {e}")
    
    async def send_telegram_alert(self, message: str):
        """Send alert to Telegram HITL bot"""
        try:
            bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
            allowed_users = os.getenv('TELEGRAM_ALLOWED_USER_IDS', '')
            
            if not bot_token or not allowed_users:
                logger.warning("⚠️  Telegram credentials not set")
                return
            
            user_ids = [uid.strip() for uid in allowed_users.split(',') if uid.strip()]
            
            async with httpx.AsyncClient() as client:
                for user_id in user_ids:
                    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    payload = {
                        'chat_id': user_id,
                        'text': f"🚨 **HEALTH MONITOR ALERT**\n\n{message}",
                        'parse_mode': 'Markdown'
                    }
                    
                    await client.post(url, json=payload)
            
            logger.info("📱 Telegram alert sent")
            
        except Exception as e:
            logger.error(f"❌ Failed to send Telegram alert: {e}")
    
    async def run_health_check(self):
        """Execute full health check cycle"""
        try:
            logger.info("🏥 Starting health check...")
            
            # Post canary tweet
            tweet_id = self.post_canary_tweet()
            
            if not tweet_id:
                logger.error("❌ Failed to post canary tweet")
                return
            
            # Wait for propagation
            await asyncio.sleep(30)
            
            # Verify from different IPs
            verification_ips = [
                None,  # Direct connection (datacenter IP)
                os.getenv('MOBILE_PROXY_1'),  # 4G proxy 1
                os.getenv('MOBILE_PROXY_2'),  # 4G proxy 2
            ]
            
            results = []
            
            for ip in verification_ips:
                if ip or ip is None:  # Skip if env var not set
                    result = await self.verify_tweet_visibility(tweet_id, ip)
                    results.append(result)
                    await asyncio.sleep(5)
            
            # Analyze results
            visible_count = sum(1 for r in results if r.get('visible') == True)
            total_checks = len(results)
            
            if visible_count == 0:
                # Complete shadowban
                test_result = 'ghost_banned'
                await self.send_telegram_alert(
                    f"🚨 CRITICAL: Complete shadowban detected!\n"
                    f"Canary tweet {tweet_id} invisible from {total_checks} verification points."
                )
            elif visible_count < total_checks:
                # Partial visibility issues
                test_result = 'search_ban'
                await self.send_telegram_alert(
                    f"⚠️  WARNING: Partial shadowban detected!\n"
                    f"Canary tweet {tweet_id} visible from {visible_count}/{total_checks} endpoints."
                )
            else:
                # All clear
                test_result = 'visible'
                logger.info(f"✅ Health check passed: {visible_count}/{total_checks} visible")
            
            # Log result
            self.log_canary_result(
                tweet_id,
                test_result,
                verification_ips[1] or 'datacenter'
            )
            
            # Delete canary tweet to avoid clutter
            try:
                self.client.delete_tweet(tweet_id)
                logger.info(f"🗑️  Deleted canary tweet {tweet_id}")
            except Exception as e:
                logger.warning(f"⚠️  Failed to delete canary: {e}")
            
        except Exception as e:
            logger.error(f"❌ Health check failed: {e}")
    
    async def run_forever(self):
        """Main loop - runs every 6 hours"""
        self.connect_db()
        self.init_twitter_client()
        
        logger.info("🚀 Health Monitor started (6h cycle)")
        
        while True:
            try:
                await self.run_health_check()
            except Exception as e:
                logger.error(f"❌ Health check cycle failed: {e}")
            
            # Run every 6 hours
            await asyncio.sleep(6 * 60 * 60)
    
    def cleanup(self):
        """Cleanup resources"""
        if self.db_conn:
            self.db_conn.close()


async def main():
    monitor = HealthMonitor()
    try:
        await monitor.run_forever()
    except KeyboardInterrupt:
        logger.info("⏹️  Shutting down Health Monitor...")
    finally:
        monitor.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
