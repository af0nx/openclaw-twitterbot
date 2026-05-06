#!/usr/bin/env python3
"""
Twitter VIP Monitor - Twitter Bot Pipeline V2
Monitors VIP Twitter accounts for engagement opportunities
Uses Scrapling DynamicSession with 4G mobile proxy rotation
"""

import asyncio
import logging
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from typing import List, Dict, Any, Optional
import os
import json
import time

from curl_cffi.requests import Session as CurlSession
from scrapling.fetchers import DynamicSession
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.cs2_constants import is_cs2_relevant as _is_cs2_relevant
from utils.cs2_constants import is_other_game as _is_other_game
from utils.db_utils import ensure_db_connection
from utils.runtime_schema import ensure_runtime_schema_extensions

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TwitterVIPMonitor:
    """Monitor VIP Twitter accounts for engagement opportunities"""
    
    # CS2 relevance keywords for filtering non-CS2 tweets
    CS2_KEYWORDS = {
        'cs2', 'cs:go', 'csgo', 'counter-strike', 'counterstrike',
        'navi', 'vitality', 'faze', 'g2', 'liquid', 'astralis', 'mouz',
        'heroic', 'fnatic', 'spirit', 'furia', 'ence', 'cloud9', 'big',
        'eternal fire', 'complexity', 'imperial', 'monte', 'saw', 'apeks',
        'major', 'blast', 'esl', 'iem', 'faceit', 'hltv', 'pgl',
        'awp', 'deagle', 'nuke', 'mirage', 'inferno', 'ancient', 'anubis',
        'dust2', 'vertigo', 'overpass', 'train',
        'roster', 'igl', 'awper', 'entry', 'lurker', 'rifler',
        'zywoo', 's1mple', 'niko', 'device', 'm0nesy', 'donk',
        'esport', 'esports', 'tournament', 'playoff', 'final', 'semi',
        'ace', 'clutch', 'eco', 'force buy', 'map pick', 'veto',
        'skin', 'sticker', 'capsule', 'knife', 'trade',
    }
    
    def __init__(self):
        self.db_conn = None
        self.session = None
        self.vip_accounts = []
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='twitter_pw')
        # Guest token GraphQL approach (no auth needed)
        self._curl = CurlSession(impersonate='chrome')
        self._guest_token = None
        self._guest_token_ts = 0
        self._user_id_cache = {}
        self._seen_tweet_ids = set()  # dedup across cycles
        self._BEARER = os.getenv('TWITTER_GQL_BEARER', 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA')
        self._GQL_FEATURES = json.dumps({
            "rweb_tipjar_consumption_enabled": True,
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "communities_web_enable_tweet_community_results_stream": True,
            "c9s_tweet_anatomy_moderator_badge_enabled": True,
            "articles_preview_enabled": True,
            "responsive_web_edit_tweet_api_enabled": True,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
            "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True,
            "responsive_web_twitter_article_tweet_consumption_enabled": True,
            "tweet_awards_web_tipping_enabled": False,
            "creator_subscriptions_quote_tweet_preview_enabled": False,
            "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "rweb_video_timestamps_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": True,
            "responsive_web_enhance_cards_enabled": False
        })

    def _refresh_guest_token(self):
        """Get/refresh Twitter guest token (valid ~3h)"""
        now = time.time()
        if self._guest_token and (now - self._guest_token_ts) < 7200:  # Refresh every 2h
            return True
        try:
            r = self._curl.post(
                'https://api.twitter.com/1.1/guest/activate.json',
                headers={'Authorization': self._BEARER}
            )
            if r.status_code == 200:
                self._guest_token = r.json()['guest_token']
                self._guest_token_ts = now
                logger.info(f"🔑 Guest token refreshed")
                return True
            logger.warning(f"⚠️  Guest token failed: {r.status_code}")
            return False
        except Exception as e:
            logger.warning(f"⚠️  Guest token error: {e}")
            return False

    def _gql_headers(self):
        return {
            'Authorization': self._BEARER,
            'x-guest-token': self._guest_token,
            'x-twitter-active-user': 'yes',
            'x-twitter-client-language': 'en',
        }

    def _resolve_user_id(self, username: str) -> Optional[str]:
        """Resolve Twitter username to numeric user ID via GraphQL"""
        if username in self._user_id_cache:
            return self._user_id_cache[username]
        try:
            variables = json.dumps({"screen_name": username})
            url = f'https://twitter.com/i/api/graphql/xc8f1g7BYqr6VTzTbvNlGw/UserByScreenName?variables={urllib.parse.quote(variables)}'
            r = self._curl.get(url, headers=self._gql_headers())
            if r.status_code == 200:
                uid = r.json().get('data', {}).get('user', {}).get('result', {}).get('rest_id')
                if uid:
                    self._user_id_cache[username] = uid
                    return uid
            logger.warning(f"⚠️  Could not resolve @{username}: {r.status_code}")
        except Exception as e:
            logger.warning(f"⚠️  User lookup failed @{username}: {e}")
        return None

    def _scrape_via_graphql(self, username: str) -> Optional[List[Dict]]:
        """Fetch timeline via Twitter GraphQL with guest token (no auth needed)"""
        if not self._refresh_guest_token():
            return None

        uid = self._resolve_user_id(username)
        if not uid:
            return None

        variables = json.dumps({
            "userId": uid, "count": 20,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": False,
            "withVoice": False, "withV2Timeline": True
        })
        url = (f'https://twitter.com/i/api/graphql/V7H0Ap3_Hh2FyS75OCDO3Q/UserTweets'
               f'?variables={urllib.parse.quote(variables)}'
               f'&features={urllib.parse.quote(self._GQL_FEATURES)}')
        try:
            r = self._curl.get(url, headers=self._gql_headers())
            if r.status_code == 429:
                logger.warning("⚠️  GraphQL rate-limited, will retry next cycle")
                return None
            if r.status_code != 200:
                logger.warning(f"⚠️  GraphQL {r.status_code} for @{username}")
                # Force guest token refresh on auth errors
                if r.status_code in (401, 403):
                    self._guest_token = None
                return None

            data = r.json()
            tweets = []
            instructions = (data.get('data', {}).get('user', {}).get('result', {})
                          .get('timeline_v2', {}).get('timeline', {}).get('instructions', []))
            for inst in instructions:
                for entry in inst.get('entries', []):
                    if len(tweets) >= 20:
                        break
                    item = entry.get('content', {}).get('itemContent', {})
                    result = item.get('tweet_results', {}).get('result', {})
                    legacy = result.get('legacy', {})
                    text = legacy.get('full_text', '')
                    tid = legacy.get('id_str', '')
                    if not text or not tid:
                        continue
                    # Skip retweets
                    if text.startswith('RT @'):
                        continue
                    metrics = legacy.get('entities', {})
                    media = legacy.get('extended_entities', {}).get('media', [])
                    pm = result.get('legacy', {})
                    tweets.append({
                        'tweet_id': tid,
                        'author': username,
                        'text': text,
                        'timestamp': legacy.get('created_at'),
                        'has_media': len(media) > 0,
                        'media_urls': [m.get('media_url_https', '') for m in media],
                        'likes': legacy.get('favorite_count', 0),
                        'replies': legacy.get('reply_count', 0),
                        'retweets': legacy.get('retweet_count', 0),
                    })
            logger.info(f"✅ @{username}: {len(tweets)} tweets via GraphQL")
            return tweets
        except Exception as e:
            logger.warning(f"⚠️  GraphQL error for @{username}: {e}")
            return None
        
    def connect_db(self):
        """Establish PostgreSQL connection"""
        try:
            self.db_conn = ensure_db_connection(self.db_conn)
            ensure_runtime_schema_extensions(self.db_conn)
            logger.info("✅ Connected to PostgreSQL")
            self.load_vip_accounts()
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise

    def _ensure_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    def _tweet_url(self, author: str, tweet_id: str) -> str:
        return f"https://twitter.com/{author}/status/{tweet_id}"

    def _tweet_already_recorded(self, tweet_id: Optional[str], author: str = '') -> bool:
        """Check durable DB state before attempting to create a new VIP event."""
        if not tweet_id or not self.db_conn:
            return False

        tweet_url = self._tweet_url(author, tweet_id) if author else None
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                if tweet_url:
                    cur.execute(
                        """
                        SELECT 1
                        FROM twitter_bot.events
                        WHERE source = 'twitter'
                          AND category = 'vip_engagement'
                          AND source_url = %s
                        LIMIT 1
                        """,
                        (tweet_url,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT 1
                        FROM twitter_bot.events
                        WHERE source = 'twitter'
                          AND category = 'vip_engagement'
                          AND metadata->>'tweet_id' = %s
                        LIMIT 1
                        """,
                        (str(tweet_id),),
                    )
                return cur.fetchone() is not None
        except Exception as e:
            logger.warning(f"⚠️  Durable dedup lookup failed for tweet {tweet_id}: {e}")
            return False
    
    def load_vip_accounts(self):
        """Load monitored VIP accounts from database"""
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT twitter_username, list_type, engagement_priority, auto_hitl_engage
                    FROM twitter_bot.monitored_accounts
                    WHERE active = TRUE
                    ORDER BY engagement_priority DESC
                """)
                self.vip_accounts = [
                    {
                        'username': row[0],
                        'list_type': row[1],
                        'priority': row[2],
                        'auto_engage': row[3]
                    }
                    for row in cur.fetchall()
                ]
                logger.info(f"📋 Loaded {len(self.vip_accounts)} VIP accounts")
        except Exception as e:
            logger.error(f"❌ Failed to load VIP accounts: {e}")
            self.vip_accounts = []
    
    async def get_mobile_proxy(self) -> Dict[str, str]:
        """Get a fresh 4G/5G mobile proxy from the rotation pool"""
        # This is a placeholder - integrate with your mobile proxy API
        # Example: https://proxy-provider.com/api/rotate
        
        proxy_api = os.getenv('MOBILE_PROXY_API_ENDPOINT')
        proxy_key = os.getenv('MOBILE_PROXY_API_KEY')
        
        if not proxy_api:
            logger.warning("⚠️  Mobile proxy not configured, using direct connection")
            return None
        
        try:
            # Placeholder for proxy rotation API call
            # In production, this would call your proxy provider's API
            return {
                'http': f'http://user:pass@proxy-ip:port',
                'https': f'http://user:pass@proxy-ip:port'
            }
        except Exception as e:
            logger.error(f"❌ Proxy rotation failed: {e}")
            return None
    
    async def scrape_user_timeline(self, username: str) -> List[Dict[str, Any]]:
        """Fetch user timeline: GraphQL guest token primary, scrapling DOM fallback"""
        # Try GraphQL guest token approach first (no auth needed, reliable)
        result = await asyncio.to_thread(self._scrape_via_graphql, username)
        if result is not None:
            return result

        # Fallback: DOM scraping via scrapling (unreliable — tweet text often empty)
        try:
            if not self.session:
                proxy = await self.get_mobile_proxy()
                # Use DynamicSession for JavaScript-heavy Twitter pages
                self.session = DynamicSession(
                    headless=True,
                    proxy=proxy,
                    user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'
                )
                await asyncio.get_event_loop().run_in_executor(self._executor, self.session.start)
            
            url = f'https://twitter.com/{username}'
            
            # Navigate to user profile
            page = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                partial(self.session.fetch, url, network_idle=True)
            )
            
            # Extract tweets using CSS selectors
            # NOTE: These selectors change frequently - auto-healing via scrapling_medic.sh
            tweets = []
            tweet_elements = page.css('[data-testid="tweet"]')
            
            for element in tweet_elements[:5]:  # Latest 5 tweets
                try:
                    text_els = element.css('[data-testid="tweetText"]')
                    tweet_text = text_els[0].text if text_els else ''
                    time_els = element.css('time')
                    tweet_time = time_els[0].attrib.get('datetime') if time_els else None
                    status_els = element.css('a[href*="/status/"]')
                    tweet_id = status_els[0].attrib.get('href', '').split('/')[-1] if status_els else None
                    
                    # Check for media
                    photo_els = element.css('[data-testid="tweetPhoto"]')
                    has_media = len(photo_els) > 0
                    media_urls = []
                    
                    if has_media:
                        media_elements = element.css('[data-testid="tweetPhoto"] img')
                        media_urls = [img.attrib.get('src') for img in media_elements]
                    
                    # Engagement metrics
                    like_els = element.css('[data-testid="like"] span')
                    likes = like_els[0].text if like_els else '0'
                    reply_els = element.css('[data-testid="reply"] span')
                    replies = reply_els[0].text if reply_els else '0'
                    rt_els = element.css('[data-testid="retweet"] span')
                    retweets = rt_els[0].text if rt_els else '0'
                    
                    tweets.append({
                        'tweet_id': tweet_id,
                        'author': username,
                        'text': tweet_text,
                        'timestamp': tweet_time,
                        'has_media': has_media,
                        'media_urls': media_urls,
                        'likes': self.parse_metric(likes),
                        'replies': self.parse_metric(replies),
                        'retweets': self.parse_metric(retweets)
                    })
                    
                except Exception as e:
                    logger.warning(f"⚠️  Failed to parse tweet element: {e}")
                    continue
            
            logger.info(f"✅ @{username}: Scraped {len(tweets)} tweets")
            return tweets
            
        except Exception as e:
            logger.error(f"❌ Failed to scrape @{username}: {e}")
            return []
    
    def parse_metric(self, text: str) -> int:
        """Parse engagement metrics (handles K, M notation)"""
        text = text.strip().upper()
        if 'K' in text:
            return int(float(text.replace('K', '')) * 1000)
        elif 'M' in text:
            return int(float(text.replace('M', '')) * 1000000)
        try:
            return int(text)
        except:
            return 0
    
    def is_cs2_relevant(self, text: str) -> bool:
        """Check if tweet content is clearly CS2-related."""
        if not text.strip():
            return False  # Can't check relevance of empty text
        if _is_other_game(text):
            return False
        return _is_cs2_relevant(text)
    
    def should_engage(self, tweet: Dict[str, Any], vip: Dict[str, str]) -> bool:
        """Determine if we should create an engagement opportunity"""
        # Don't engage with retweets or replies
        if tweet['text'].startswith('RT @') or tweet['text'].startswith('@'):
            return False
        
        # Skip tweets with no text (DOM scraping couldn't extract content)
        if not tweet['text'].strip():
            logger.info(f"⏭️  @{tweet['author']}: empty tweet text (scraping limitation)")
            return False

        if os.getenv('TWITTER_REQUIRE_SOURCE_MEDIA', 'true').lower() in ('1', 'true', 'yes', 'on'):
            if not tweet.get('has_media'):
                logger.info(f"⏭️  @{tweet['author']}: source tweet has no media")
                return False

        # We need the source tweet ID for durable dedup and safe reply targeting.
        tid = tweet.get('tweet_id')
        if not tid:
            logger.info(f"⏭️  @{tweet['author']}: missing tweet_id")
            return False
        
        # Dedup: skip tweets we already processed in a previous cycle
        if tid and (tid in self._seen_tweet_ids or self._tweet_already_recorded(tid, tweet.get('author', ''))):
            return False
        
        # Only reply to tweets that are 5-120 minutes old (widened window for reliable capture)
        try:
            ts = tweet.get('timestamp')
            if ts:
                tweet_time = datetime.strptime(ts, '%a %b %d %H:%M:%S %z %Y')
                age_minutes = (datetime.now(timezone.utc) - tweet_time).total_seconds() / 60
                if age_minutes < 5:
                    logger.debug(f"⏭️  @{tweet['author']}: tweet too fresh ({age_minutes:.0f}m old, need 5m+)")
                    return False
                if age_minutes > 120:
                    logger.debug(f"⏭️  @{tweet['author']}: tweet too old ({age_minutes:.0f}m old, max 120m)")
                    return False
                logger.info(f"✅ @{tweet['author']}: tweet age {age_minutes:.0f}m (5-120m window)")
        except Exception as e:
            logger.warning(f"⚠️  Could not parse tweet timestamp for @{tweet['author']}: {e}")
        
        # Skip non-CS2 tweets (avoids spam about unrelated topics)
        if not self.is_cs2_relevant(tweet['text']):
            logger.info(f"⏭️  Skipping non-CS2 tweet from @{tweet['author']}")
            return False
        
        # High-priority accounts: always consider
        if vip['priority'] >= 8:
            return True
        
        # For lower priority, check if tweet is gaining traction
        if tweet['likes'] < 10 and tweet['replies'] < 3:
            return False
        
        return True
    
    def store_engagement_opportunity(self, tweet: Dict[str, Any], vip: Dict[str, str]):
        """Store VIP tweet as potential engagement opportunity"""
        try:
            self._ensure_db()
            tweet_id = tweet.get('tweet_id')
            tweet_author = tweet.get('author', '')
            tweet_url = self._tweet_url(tweet_author, tweet_id) if tweet_id and tweet_author else None
            if not tweet_url:
                logger.info(f"⏭️  Skipping VIP tweet without durable source URL: @{tweet_author}")
                return
            if tweet_id and self._tweet_already_recorded(tweet_id, tweet_author):
                self._seen_tweet_ids.add(tweet_id)
                logger.info(f"⏭️  Skipping already-recorded VIP tweet: @{tweet_author}")
                return

            with self.db_conn.cursor() as cur:
                # First, create a Siftly event if media present
                siftly_event_id = None
                if tweet['has_media']:
                    cur.execute("""
                        INSERT INTO twitter_bot.siftly_events
                        (source_url, vip_author, raw_text, has_media, media_urls)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id
                    """, (
                        tweet_url,
                        tweet['author'],
                        tweet['text'],
                        True,
                        Json(tweet['media_urls'])
                    ))
                    siftly_event_id = cur.fetchone()[0]
                
                # Create event
                cur.execute("""
                    INSERT INTO twitter_bot.events
                    (siftly_event_id, headline, content, source, source_url, category, urgency, status, metadata)
                    VALUES (%s, %s, %s, 'twitter', %s, %s, %s, 'pending', %s)
                    ON CONFLICT DO NOTHING
                """, (
                    siftly_event_id,
                    f"VIP Tweet: @{tweet['author']}",
                    tweet['text'],
                    tweet_url,
                    'vip_engagement',
                    'important' if vip['priority'] >= 8 else 'normal',
                    Json({
                        'tweet_id': tweet['tweet_id'],
                        'vip_username': tweet['author'],
                        'vip_list_type': vip['list_type'],
                        'engagement_priority': vip['priority'],
                        'auto_hitl_engage': vip['auto_engage'],
                        'source_has_media': bool(tweet.get('has_media')),
                        'source_media_urls': tweet.get('media_urls') or [],
                        'engagement_metrics': {
                            'likes': tweet['likes'],
                            'replies': tweet['replies'],
                            'retweets': tweet['retweets']
                        }
                    })
                ))
                
                self.db_conn.commit()
                if tweet.get('tweet_id'):
                    self._seen_tweet_ids.add(tweet['tweet_id'])
                logger.info(f"💾 Stored engagement opportunity: @{tweet['author']}")
                
        except Exception as e:
            logger.error(f"❌ Failed to store engagement: {e}")
            self.db_conn.rollback()
    
    async def monitor_vip(self, vip: Dict[str, str]):
        """Monitor a single VIP account"""
        tweets = await self.scrape_user_timeline(vip['username'])
        
        for tweet in tweets:
            if self.should_engage(tweet, vip):
                self.store_engagement_opportunity(tweet, vip)
        
        # Update last checked timestamp
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE twitter_bot.monitored_accounts
                    SET last_checked_at = NOW()
                    WHERE twitter_username = %s
                """, (vip['username'],))
                self.db_conn.commit()
        except Exception as e:
            logger.error(f"❌ Failed to update last_checked: {e}")
    
    async def run_cycle(self):
        """Run one monitoring cycle across all VIPs"""
        logger.info("🔄 Starting VIP monitoring cycle...")
        self._ensure_db()
        
        # Prune dedup set — keep only recent tweet IDs (prevent memory leak)
        if len(self._seen_tweet_ids) > 5000:
            self._seen_tweet_ids = set(list(self._seen_tweet_ids)[-1000:])
            logger.info("🧹 Trimmed local dedup cache to 1000 entries")
        
        failed = 0
        for vip in self.vip_accounts:
            try:
                await self.monitor_vip(vip)
                # Stagger requests — 2s between accounts (16 accounts ≈ 32s total)
                await asyncio.sleep(2)
            except Exception as e:
                failed += 1
                logger.error(f"❌ Failed to monitor @{vip['username']}: {e}")
        
        logger.info(f"✅ Completed VIP cycle ({len(self.vip_accounts)} accounts, {failed} failed)")
        
        # If >50% of accounts failed, force guest token refresh for next cycle
        if failed > len(self.vip_accounts) / 2:
            logger.warning("⚠️  Majority of accounts failed — forcing guest token refresh")
            self._guest_token = None
    
    async def run_forever(self):
        """Main loop - runs every 300 seconds (5 minutes)"""
        self.connect_db()
        logger.info("🚀 Twitter VIP Monitor started")
        
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"❌ Cycle failed: {e}")
                # Force guest token refresh on full cycle crash
                self._guest_token = None
            
            # Wait 5 minutes between cycles
            await asyncio.sleep(300)
    
    def cleanup(self):
        """Cleanup resources"""
        if self.session:
            self.session.close()
        if self.db_conn:
            self.db_conn.close()


async def main():
    monitor = TwitterVIPMonitor()
    try:
        await monitor.run_forever()
    except KeyboardInterrupt:
        logger.info("⏹️  Shutting down Twitter VIP Monitor...")
    finally:
        monitor.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
