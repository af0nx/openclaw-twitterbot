#!/usr/bin/env python3
"""
Community Liker - Twitter Bot Pipeline V2
Proactively likes tweets from CS2 community accounts and people who interact with us.
This builds relationships, signals engagement to the algorithm, and grows the account.

Strategy priority:
    1. Like VIP/monitored account tweets (relationship building)
    2. Like posts we already engaged with (durable DB-backed targets)
    3. Like CS2 community account timelines (community presence)

Reply-search lookups for conversation replies are disabled by default because
Twitter's adaptive search endpoint is currently returning 404s for this flow.

Conservative limits to avoid suspension:
  - Max 200 likes/day (X allows ~500, we stay well under)
  - Max 40 likes per cycle
  - 3-8 second random delay between likes (human-like pacing)
  - Never like the same tweet twice (DB dedup)
"""

import asyncio
import json
import logging
import os
import random
import time
import urllib.parse
from datetime import datetime, timezone
from typing import List, Optional, Dict

from dotenv import load_dotenv
import psycopg2
import tweepy
from curl_cffi.requests import Session as CurlSession

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.db_utils import ensure_db_connection
from utils.twitter_accounts import get_account_credentials

load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# CS2 keywords for search-based liking
CS2_KEYWORDS = [
    'CS2', 'Counter-Strike 2', '#CS2',
    'HLTV', 'CS2 Major', 'IEM', 'BLAST',
    'FaZe CS', 'NaVi CS', 'Vitality CS', 'Spirit CS',
    's1mple', 'ZywOo', 'donk', 'm0NESY', 'NiKo',
]

DAILY_LIKE_CAP = int(os.getenv('DAILY_LIKE_CAP', '200'))
LIKES_PER_CYCLE = int(os.getenv('LIKES_PER_CYCLE', '40'))
LIKE_DELAY_MIN_SECONDS = float(os.getenv('LIKE_DELAY_MIN_SECONDS', '3'))
LIKE_DELAY_MAX_SECONDS = float(os.getenv('LIKE_DELAY_MAX_SECONDS', '8'))
SEARCH_LIKE_DELAY_MIN_SECONDS = float(os.getenv('SEARCH_LIKE_DELAY_MIN_SECONDS', '4'))
SEARCH_LIKE_DELAY_MAX_SECONDS = float(os.getenv('SEARCH_LIKE_DELAY_MAX_SECONDS', '10'))
PEAK_LIKE_INTERVAL_MINUTES = int(os.getenv('PEAK_LIKE_INTERVAL_MINUTES', '45'))
OFFPEAK_LIKE_INTERVAL_MINUTES = int(os.getenv('OFFPEAK_LIKE_INTERVAL_MINUTES', '90'))


class CommunityLiker:
    """Proactive community engagement via likes"""

    def __init__(self):
        self.db_conn = None
        self.client = None
        self._api_disabled = False
        self._our_user_id = None
        # GraphQL scraping (no API tier needed)
        self._curl = CurlSession(impersonate='chrome')
        self._guest_token = None
        self._guest_token_ts = 0
        self._user_id_cache: Dict[str, str] = {}
        self._reply_search_enabled = os.getenv('COMMUNITY_LIKER_ENABLE_REPLY_SEARCH', 'false').lower() in ('1', 'true', 'yes', 'on')
        self._reply_search_disabled_logged = False
        self._GQL_BEARER = os.getenv('TWITTER_GQL_BEARER', 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA')
        self._GQL_FEATURES = json.dumps({
            "rweb_tipjar_consumption_enabled": True,
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "communities_web_enable_tweet_community_results_stream": True,
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
            "responsive_web_media_download_video_enabled": False,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": True,
            "responsive_web_enhance_cards_enabled": False,
        })
        self.init_twitter_client()

    def init_twitter_client(self):
        """Initialize Tweepy v2 client with OAuth 1.0a (needed for like endpoint)"""
        try:
            creds = get_account_credentials('replies')
            api_key = creds['api_key']
            api_secret = creds['api_secret']
            access_token = creds['access_token']
            access_secret = creds['access_secret']
            bearer_token = creds['bearer_token']

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

            # Get our own user ID (needed for like endpoint)
            me = self.client.get_me()
            if me and me.data:
                self._our_user_id = me.data.id
                logger.info(f"✅ Twitter client initialized — user ID: {self._our_user_id}")
            else:
                raise ValueError("Could not fetch own user ID")

        except Exception as e:
            logger.error(f"❌ Failed to initialize Twitter client: {e}")
            raise

    def connect_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    def _ensure_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    def _already_liked(self, tweet_id: str) -> bool:
        """Check if we already liked this tweet"""
        with self.db_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM twitter_bot.community_likes WHERE twitter_tweet_id = %s",
                (str(tweet_id),)
            )
            return cur.fetchone() is not None

    def _record_like(self, tweet_id: str, author: str, source: str):
        """Record a like in the DB"""
        with self.db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO twitter_bot.community_likes (twitter_tweet_id, author_username, source)
                VALUES (%s, %s, %s)
                ON CONFLICT (twitter_tweet_id) DO NOTHING
            """, (str(tweet_id), author, source))
            self.db_conn.commit()

    def _get_daily_likes(self) -> int:
        """Get number of likes given today"""
        with self.db_conn.cursor() as cur:
            cur.execute("""
                SELECT likes_given FROM twitter_bot.like_quotas
                WHERE date = CURRENT_DATE
            """)
            row = cur.fetchone()
            return row[0] if row else 0

    def _increment_like_count(self):
        """Increment today's like counter"""
        with self.db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO twitter_bot.like_quotas (date, likes_given)
                VALUES (CURRENT_DATE, 1)
                ON CONFLICT (date) DO UPDATE
                SET likes_given = twitter_bot.like_quotas.likes_given + 1,
                    updated_at = NOW()
            """)
            self.db_conn.commit()

    def _do_like(self, tweet_id: str, author: str, source: str) -> bool:
        """Actually like a tweet via the API. Returns True if successful."""
        if self._api_disabled:
            return False

        tweet_id_str = str(tweet_id)

        if self._already_liked(tweet_id_str):
            return False

        try:
            self.client.like(tweet_id_str)
            self._record_like(tweet_id_str, author, source)
            self._increment_like_count()
            logger.info(f"❤️  Liked tweet {tweet_id_str[:15]}... by @{author} ({source})")
            return True

        except tweepy.TweepyException as e:
            err = str(e)
            if '401' in err or '403' in err:
                if 'already liked' in err.lower() or 'You have already' in err:
                    # Already liked — record it so we skip next time
                    self._record_like(tweet_id_str, author, source)
                    return False
                logger.warning(f"⚠️  Like API disabled: {err[:80]}")
                self._api_disabled = True
                return False
            if '429' in err:
                logger.warning(f"⚠️  Rate limited on likes — backing off")
                return False
            logger.error(f"❌ Like failed: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Unexpected like error: {e}")
            return False

    def _dedupe_candidates(self, candidates: List[dict]) -> List[dict]:
        seen = set()
        deduped = []
        for candidate in candidates:
            tweet_id = str(candidate.get('tweet_id') or '').strip()
            if not tweet_id or tweet_id in seen:
                continue
            seen.add(tweet_id)
            deduped.append(candidate)
        return deduped

    # ─── GraphQL Tweet Discovery (no API tier required) ──────────

    def _refresh_guest_token(self) -> bool:
        """Get/refresh Twitter guest token (valid ~3h)"""
        now = time.time()
        if self._guest_token and (now - self._guest_token_ts) < 7200:
            return True
        try:
            r = self._curl.post(
                'https://api.twitter.com/1.1/guest/activate.json',
                headers={'Authorization': self._GQL_BEARER},
                timeout=10,
            )
            if r.status_code == 200:
                self._guest_token = r.json()['guest_token']
                self._guest_token_ts = now
                logger.info("🔑 Guest token refreshed")
                return True
            logger.warning(f"⚠️  Guest token failed: {r.status_code}")
            return False
        except Exception as e:
            logger.warning(f"⚠️  Guest token error: {e}")
            return False

    def _gql_headers(self) -> dict:
        return {
            'Authorization': self._GQL_BEARER,
            'x-guest-token': self._guest_token,
            'x-twitter-active-user': 'yes',
            'x-twitter-client-language': 'en',
        }

    def _resolve_user_id(self, username: str) -> Optional[str]:
        """Resolve Twitter username → numeric user ID via GraphQL"""
        if username in self._user_id_cache:
            return self._user_id_cache[username]
        try:
            variables = json.dumps({"screen_name": username})
            url = f'https://twitter.com/i/api/graphql/xc8f1g7BYqr6VTzTbvNlGw/UserByScreenName?variables={urllib.parse.quote(variables)}'
            r = self._curl.get(url, headers=self._gql_headers(), timeout=10)
            if r.status_code == 200:
                uid = r.json().get('data', {}).get('user', {}).get('result', {}).get('rest_id')
                if uid:
                    self._user_id_cache[username] = uid
                    return uid
            logger.info(f"Could not resolve @{username}: {r.status_code}")
        except Exception as e:
            logger.info(f"User lookup failed @{username}: {e}")
        return None

    def _extract_tweets_from_gql(self, data: dict, author_hint: str = '') -> List[dict]:
        """Parse Twitter GraphQL response into tweet dicts"""
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
                if text.startswith('RT @'):
                    continue
                core = result.get('core', {}).get('user_results', {}).get('result', {})
                screen_name = core.get('legacy', {}).get('screen_name', author_hint)
                tweets.append({
                    'tweet_id': tid,
                    'author': screen_name or author_hint,
                    'text': text,
                    'likes': legacy.get('favorite_count', 0),
                })
        return tweets

    def _scrape_vip_timeline(self, username: str) -> List[dict]:
        """Fetch VIP tweets via GraphQL (bypasses API tier restrictions)"""
        if not self._refresh_guest_token():
            return []
        uid = self._resolve_user_id(username)
        if not uid:
            return []
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
            r = self._curl.get(url, headers=self._gql_headers(), timeout=15)
            if r.status_code == 429:
                logger.warning("⚠️  GraphQL rate-limited — backing off")
                return []
            if r.status_code in (401, 403):
                logger.info(f"GraphQL timeline @{username}: {r.status_code} — resetting guest token")
                self._guest_token = None
                return []
            if r.status_code != 200:
                logger.info(f"GraphQL timeline @{username}: unexpected status {r.status_code}")
                return []
            tweets = self._extract_tweets_from_gql(r.json(), author_hint=username)
            logger.info(f"📡 GraphQL timeline @{username}: {len(tweets)} tweets, status={r.status_code}")
            return tweets
        except Exception as e:
            logger.info(f"GraphQL timeline error for @{username}: {e}")
            return []

    def _scrape_cs2_search(self, query: str) -> List[dict]:
        """Search Twitter for tweets via adaptive search API (bypasses API tier)"""
        if not self._refresh_guest_token():
            return []
        params = urllib.parse.urlencode({
            'q': query,
            'tweet_search_mode': 'live',
            'count': '20',
            'query_source': 'typed_query',
            'pc': '1',
            'spelling_corrections': '1',
        })
        url = f'https://twitter.com/i/api/2/search/adaptive.json?{params}'
        try:
            r = self._curl.get(url, headers=self._gql_headers(), timeout=15)
            if r.status_code == 429:
                logger.warning("⚠️  Search rate-limited")
                return []
            if r.status_code in (401, 403):
                logger.info(f"Search {r.status_code} — resetting guest token")
                self._guest_token = None
                return []
            if r.status_code == 404:
                logger.warning("⚠️  Adaptive search returned 404 — disabling reply search strategy")
                self._reply_search_enabled = False
                return []
            if r.status_code != 200:
                logger.info(f"Search unexpected status: {r.status_code}")
                return []
            tweets = []
            data = r.json()
            global_tweets = data.get('globalObjects', {}).get('tweets', {})
            global_users = data.get('globalObjects', {}).get('users', {})
            for tid, tw in global_tweets.items():
                if len(tweets) >= 20:
                    break
                text = tw.get('full_text', '')
                if not text or text.startswith('RT @'):
                    continue
                uid = str(tw.get('user_id_str', ''))
                user = global_users.get(uid, {})
                screen_name = user.get('screen_name', 'unknown')
                tweets.append({
                    'tweet_id': tid,
                    'author': screen_name,
                    'text': text,
                    'likes': tw.get('favorite_count', 0),
                })
            logger.info(f"🔍 Search '{query[:40]}': {len(tweets)} tweets, status={r.status_code}")
            return tweets
        except Exception as e:
            logger.info(f"Search error: {e}")
            return []

    # ─── Strategy 1: Like replies to our tweets ──────────────────

    def get_reply_tweets_to_like(self) -> List[dict]:
        """
        Find tweets that replied to our tweets.
        Uses GraphQL search with conversation_id to discover replies
        (no API tier needed — guest token approach).
        """
        if not self._reply_search_enabled:
            if not self._reply_search_disabled_logged:
                logger.info("📬 Reply search strategy disabled — adaptive search conversation lookups are unavailable")
                self._reply_search_disabled_logged = True
            return []

        tweets_to_like = []
        try:
            self._ensure_db()

            # Get IDs of our recent posted tweets
            with self.db_conn.cursor() as cur:
                cur.execute("SET statement_timeout = '15s'")
                cur.execute("""
                    SELECT twitter_tweet_id
                    FROM twitter_bot.tweets_v2
                    WHERE status = 'posted'
                    AND twitter_tweet_id IS NOT NULL
                    AND posted_at > NOW() - INTERVAL '3 days'
                    ORDER BY posted_at DESC
                    LIMIT 10
                """)
                our_tweet_ids = [row[0] for row in cur.fetchall()]
                cur.execute("RESET statement_timeout")

            logger.info(f"📬 Found {len(our_tweet_ids)} of our tweets to check for replies")

            if our_tweet_ids:
                for our_tweet_id in our_tweet_ids[:3]:
                    try:
                        query = f"conversation_id:{our_tweet_id}"
                        scraped = self._scrape_cs2_search(query)
                        for tweet in scraped:
                            if str(tweet.get('tweet_id')) == str(our_tweet_id):
                                continue  # Skip our own tweet
                            tweets_to_like.append({
                                'tweet_id': tweet['tweet_id'],
                                'author': tweet['author'],
                                'source': 'reply_to_us'
                            })
                        time.sleep(1)
                    except Exception as e:
                        logger.info(f"Reply search failed for {our_tweet_id}: {e}")

        except Exception as e:
            logger.error(f"❌ Reply fetch failed: {e}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass

        logger.info(f"📬 Reply strategy: {len(tweets_to_like)} candidates")
        return self._dedupe_candidates(tweets_to_like)

    # ─── Strategy 2: Like VIP account tweets ─────────────────────

    def get_vip_tweets_to_like(self) -> List[dict]:
        """
        Like VIP tweets we already know about from the events table.
        twitter_monitor scrapes these via GraphQL and stores tweet_id in metadata.
        No additional API calls needed — we use what we already scraped.
        
        Also tries timeline API as a bonus if available.
        """
        tweets_to_like = []
        self._ensure_db()

        # Primary: Use VIP tweet IDs already in our events table (from GraphQL scraping)
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("SET statement_timeout = '15s'")
                cur.execute("""
                    SELECT metadata->>'tweet_id' as tweet_id,
                           metadata->>'vip_username' as author
                    FROM twitter_bot.events
                    WHERE category = 'vip_engagement'
                    AND metadata->>'tweet_id' IS NOT NULL
                    AND created_at > NOW() - INTERVAL '3 days'
                    ORDER BY created_at DESC
                    LIMIT 30
                """)
                for row in cur.fetchall():
                    if row[0]:
                        tweets_to_like.append({
                            'tweet_id': row[0],
                            'author': row[1] or 'unknown',
                            'source': 'vip_from_events'
                        })
                cur.execute("RESET statement_timeout")
        except Exception as e:
            logger.info(f"VIP events query failed: {e}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass

        # Also try: VIP tweets that we replied to (our reply_target_id)
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("SET statement_timeout = '15s'")
                cur.execute("""
                    SELECT reply_target_id
                    FROM twitter_bot.tweets_v2
                    WHERE reply_target_id IS NOT NULL
                    AND pillar = 12
                    AND posted_at > NOW() - INTERVAL '7 days'
                    ORDER BY posted_at DESC
                    LIMIT 20
                """)
                for row in cur.fetchall():
                    if row[0]:
                        tweets_to_like.append({
                            'tweet_id': row[0],
                            'author': 'vip',
                            'source': 'vip_replied_to'
                        })
                cur.execute("RESET statement_timeout")
        except Exception as e:
            logger.info(f"VIP replies query failed: {e}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass

        logger.info(f"📊 VIP DB: {len(tweets_to_like)} candidates from events")

        # Bonus: Scrape VIP timelines via GraphQL (no API tier required)
        if len(tweets_to_like) < 10:
            logger.info(f"📡 VIP GraphQL fallback triggered — {len(tweets_to_like)} DB candidates < 10")
            try:
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        SELECT twitter_username
                        FROM twitter_bot.monitored_accounts
                        WHERE active = TRUE
                        ORDER BY engagement_priority DESC
                        LIMIT 8
                    """)
                    vips = [row[0] for row in cur.fetchall()]
            except Exception as e:
                logger.info(f"VIP monitored_accounts query failed: {e}")
                vips = []
                try:
                    self.db_conn.rollback()
                except Exception:
                    pass

            random.shuffle(vips)
            for username in vips[:6]:
                try:
                    scraped = self._scrape_vip_timeline(username)
                    for tw in scraped[:5]:
                        tweets_to_like.append({
                            'tweet_id': tw['tweet_id'],
                            'author': tw['author'],
                            'source': 'vip_timeline'
                        })
                    if scraped:
                        logger.info(f"📡 @{username}: {len(scraped)} tweets via GraphQL")
                    time.sleep(2)
                except Exception as e:
                    logger.info(f"GraphQL fallback failed for @{username}: {e}")

        logger.info(f"📊 VIP strategy: {len(tweets_to_like)} total candidates")
        return self._dedupe_candidates(tweets_to_like)

    def get_recent_engagement_targets_to_like(self) -> List[dict]:
        """
        Like posts and comments we already decided to engage with.
        This reinforces replies/quotes we send and works without search access.
        """
        tweets_to_like = []
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute("SET statement_timeout = '15s'")
                cur.execute("""
                    SELECT COALESCE(reply_target_id, quote_tweet_id) AS target_id,
                           CASE
                               WHEN pillar = 16 THEN 'engaged_disagreement_target'
                               WHEN reply_target_id IS NOT NULL THEN 'engaged_reply_target'
                               ELSE 'engaged_quote_target'
                           END AS source
                    FROM twitter_bot.tweets_v2
                    WHERE (reply_target_id IS NOT NULL OR quote_tweet_id IS NOT NULL)
                    AND created_at > NOW() - INTERVAL '7 days'
                    ORDER BY created_at DESC
                    LIMIT 40
                """)

                for row in cur.fetchall():
                    if row[0]:
                        tweets_to_like.append({
                            'tweet_id': str(row[0]),
                            'author': 'engaged',
                            'source': row[1],
                        })
                cur.execute("RESET statement_timeout")
        except Exception as e:
            logger.error(f"❌ Recent engagement target fetch failed: {e}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass

        logger.info(f"🎯 Engagement target strategy: {len(tweets_to_like)} candidates")
        return self._dedupe_candidates(tweets_to_like)

    # ─── Strategy 3: Like more CS2 community account tweets ─────

    # Extra CS2 accounts to scrape (beyond monitored_accounts VIPs)
    _CS2_EXTRA_ACCOUNTS = [
        'EliGE', 'Twistzz', 'Brehze', 'nilocsgo', 'Karrigancsgo',
        'gaborcsgo', 'rainCS', 'TeamLiquidCS', 'Cloud9',
        'BIGCLANgg', 'Complexity', 'OGesports', 'PGL_Esports',
        'LOABORVRE_CS2', 'ESLCS', 'FACEIT',
    ]

    def get_cs2_search_tweets_to_like(self) -> List[dict]:
        """
        Scrape additional CS2 community accounts for fresh tweets to like.
        Guest token search is blocked, so we scrape timelines instead.
        """
        tweets_to_like = []
        try:
            accounts = list(self._CS2_EXTRA_ACCOUNTS)
            random.shuffle(accounts)
            for username in accounts[:3]:
                try:
                    scraped = self._scrape_vip_timeline(username)
                    for tw in scraped[:5]:
                        tweets_to_like.append({
                            'tweet_id': tw['tweet_id'],
                            'author': tw['author'],
                            'source': f'cs2_community:{username}'
                        })
                    if scraped:
                        logger.info(f"🌐 @{username}: {len(scraped)} tweets via timeline scrape")
                    time.sleep(2)
                except Exception as e:
                    logger.info(f"CS2 community scrape failed for @{username}: {e}")

        except Exception as e:
            logger.warning(f"⚠️  CS2 community scrape failed: {e}")

        return self._dedupe_candidates(tweets_to_like)

    # ─── Main Cycle ──────────────────────────────────────────────

    async def run_cycle(self):
        """Run one like cycle across all strategies"""
        self._ensure_db()
        try:
            self.db_conn.rollback()
        except Exception:
            pass

        if self._api_disabled:
            logger.warning("⏸️  API disabled — skipping cycle")
            return

        daily_count = self._get_daily_likes()
        remaining_today = DAILY_LIKE_CAP - daily_count
        if remaining_today <= 0:
            logger.info(f"📊 Daily like cap reached ({daily_count}/{DAILY_LIKE_CAP})")
            return

        cycle_budget = min(LIKES_PER_CYCLE, remaining_today)
        liked = 0
        logger.info(f"💫 Like cycle starting — {daily_count}/{DAILY_LIKE_CAP} today, budget: {cycle_budget}")

        # Strategy 1: Optional reply search (disabled by default until the endpoint is reliable)
        if liked < cycle_budget:
            reply_tweets = self.get_reply_tweets_to_like()
            random.shuffle(reply_tweets)
            for t in reply_tweets:
                if liked >= cycle_budget:
                    break
                if self._do_like(t['tweet_id'], t['author'], t['source']):
                    liked += 1
                    await asyncio.sleep(random.uniform(LIKE_DELAY_MIN_SECONDS, LIKE_DELAY_MAX_SECONDS))

        # Strategy 2: VIP account tweets
        if liked < cycle_budget:
            vip_tweets = self.get_vip_tweets_to_like()
            random.shuffle(vip_tweets)
            for t in vip_tweets:
                if liked >= cycle_budget:
                    break
                if self._do_like(t['tweet_id'], t['author'], t['source']):
                    liked += 1
                    await asyncio.sleep(random.uniform(LIKE_DELAY_MIN_SECONDS, LIKE_DELAY_MAX_SECONDS))

        # Strategy 3: Like posts/comments we already engaged with
        if liked < cycle_budget:
            engagement_targets = self.get_recent_engagement_targets_to_like()
            random.shuffle(engagement_targets)
            for t in engagement_targets:
                if liked >= cycle_budget:
                    break
                if self._do_like(t['tweet_id'], t['author'], t['source']):
                    liked += 1
                    await asyncio.sleep(random.uniform(LIKE_DELAY_MIN_SECONDS, LIKE_DELAY_MAX_SECONDS))

        # Strategy 4: CS2 keyword search
        if liked < cycle_budget:
            search_tweets = self.get_cs2_search_tweets_to_like()
            random.shuffle(search_tweets)
            for t in search_tweets:
                if liked >= cycle_budget:
                    break
                if self._do_like(t['tweet_id'], t['author'], t['source']):
                    liked += 1
                    await asyncio.sleep(random.uniform(SEARCH_LIKE_DELAY_MIN_SECONDS, SEARCH_LIKE_DELAY_MAX_SECONDS))

        logger.info(f"✅ Like cycle complete — liked {liked} tweets ({daily_count + liked}/{DAILY_LIKE_CAP} today)")

    async def run_forever(self):
        """Main loop — runs every 45 minutes during peak, every 90 minutes off-peak"""
        self.connect_db()
        logger.info("🚀 Community Liker started — building relationships")

        consecutive_errors = 0
        while True:
            try:
                await self.run_cycle()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"❌ Cycle failed: {e}")
                if consecutive_errors >= 3:
                    logger.warning(f"⏸️  {consecutive_errors} errors — sleeping 30 min")
                    await asyncio.sleep(1800)
                    consecutive_errors = 0
                    continue

            now = datetime.now(timezone.utc)
            if 14 <= now.hour <= 23:
                await asyncio.sleep(PEAK_LIKE_INTERVAL_MINUTES * 60)
            else:
                await asyncio.sleep(OFFPEAK_LIKE_INTERVAL_MINUTES * 60)


if __name__ == '__main__':
    asyncio.run(CommunityLiker().run_forever())
