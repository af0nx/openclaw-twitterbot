#!/usr/bin/env python3
"""
Engagement Tracker - Twitter Bot Pipeline V2
Fetches metrics for posted tweets every 30 minutes.
Uses GraphQL guest token scraping (free, no API tier needed).
Stores snapshots in engagement_tracking table.
Updates tweets_v2 with latest impressions/likes/retweets.
Feeds back top-performing patterns to the content generator via RLHF labels.
"""

import asyncio
import json
import logging
import os
import time
import urllib.parse

from dotenv import load_dotenv
import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.db_utils import ensure_db_connection
from utils.runtime_schema import ensure_runtime_schema_extensions, refresh_runtime_rollups

try:
    from curl_cffi.requests import Session as CurlSession
except ImportError:
    from requests import Session as CurlSession

load_dotenv('/dev/shm/.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Our Twitter account username
OUR_USERNAME = os.getenv('TWITTER_USERNAME', 'SkinBetHub')


class EngagementTracker:
    def __init__(self):
        self.db_conn = None
        self._schema_ready = False
        self._curl = CurlSession(impersonate='chrome')
        self._guest_token = None
        self._guest_token_ts = 0
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
        })
        # GraphQL operation hash for TweetResultByRestId (per-tweet lookup)
        self._TWEET_DETAIL_OP = 'DJS3BdhUhcaEpZ7B7irJDg'

    def connect_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)
        if not self._schema_ready:
            ensure_runtime_schema_extensions(self.db_conn)
            self._schema_ready = True

    def _refresh_guest_token(self, force: bool = False) -> bool:
        now = time.time()
        if not force and self._guest_token and (now - self._guest_token_ts) < 7200:
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

    def _fetch_tweet_metrics(self, tweet_id: str) -> dict | None:
        """Fetch metrics for a single tweet via TweetResultByRestId GraphQL endpoint."""
        variables = json.dumps({
            'tweetId': tweet_id,
            'withCommunity': False,
            'includePromotedContent': False,
            'withVoice': False,
        })
        url = (f'https://twitter.com/i/api/graphql/{self._TWEET_DETAIL_OP}/TweetResultByRestId'
               f'?variables={urllib.parse.quote(variables)}'
               f'&features={urllib.parse.quote(self._GQL_FEATURES)}')
        try:
            r = self._curl.get(url, headers=self._gql_headers(), timeout=10)
            if r.status_code in (401, 403):
                # Token likely expired — force refresh and retry once
                if self._refresh_guest_token(force=True):
                    r = self._curl.get(url, headers=self._gql_headers(), timeout=10)
            if r.status_code == 429:
                logger.warning("⚠️  Rate limited (429), pausing cycle")
                return 'RATE_LIMITED'
            if r.status_code != 200:
                return None
            result = r.json().get('data', {}).get('tweetResult', {}).get('result', {})
            legacy = result.get('legacy', {})
            if not legacy:
                return None
            views = result.get('views', {})
            return {
                'tweet_id': tweet_id,
                'impressions': int(views.get('count', 0) or 0),
                'likes': legacy.get('favorite_count', 0),
                'replies': legacy.get('reply_count', 0),
                'retweets': legacy.get('retweet_count', 0),
                'quotes': legacy.get('quote_count', 0),
                'bookmarks': legacy.get('bookmark_count', 0),
            }
        except Exception as e:
            logger.debug(f"⚠️  Tweet {tweet_id} lookup failed: {e}")
            return None

    def fetch_and_store(self):
        """Fetch metrics for all posted tweets via per-tweet GraphQL lookups."""
        try:
            self.db_conn = ensure_db_connection(self.db_conn)

            if not self._refresh_guest_token():
                logger.warning("⚠️  Could not get guest token, skipping cycle")
                return

            # Get our posted tweets from DB (last 7 days)
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT id, twitter_tweet_id
                    FROM twitter_bot.tweets_v2
                    WHERE status = 'posted'
                      AND twitter_tweet_id IS NOT NULL
                      AND posted_at > NOW() - INTERVAL '7 days'
                """)
                rows = cur.fetchall()

            if not rows:
                return

            matched = 0
            for db_id, twitter_id in rows:
                metrics = self._fetch_tweet_metrics(twitter_id)
                if metrics == 'RATE_LIMITED':
                    break
                if not metrics:
                    continue

                impressions = metrics['impressions']
                likes = metrics['likes']
                replies = metrics['replies']
                retweets = metrics['retweets']
                quotes = metrics['quotes']
                bookmarks = metrics['bookmarks']
                total = likes + replies + retweets + quotes + bookmarks
                rate = (total / impressions * 100) if impressions > 0 else 0.0

                try:
                    with self.db_conn.cursor() as cur:
                        # Time-series snapshot (one row per check)
                        cur.execute("""
                            INSERT INTO twitter_bot.engagement_tracking
                            (tweet_v2_id, twitter_tweet_id, impressions, likes, replies,
                             retweets, quotes, bookmarks, engagement_rate)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (db_id, twitter_id, impressions, likes, replies,
                              retweets, quotes, bookmarks, rate))

                        # Update main record with latest metrics
                        cur.execute("""
                            UPDATE twitter_bot.tweets_v2
                            SET impressions = %s, likes = %s, replies = %s, retweets = %s,
                                quotes = %s, bookmarks = %s, engagement_rate = %s,
                                updated_at = NOW()
                            WHERE id = %s
                        """, (impressions, likes, replies, retweets, quotes, bookmarks, rate, db_id))

                        # Auto-label RLHF (don't overwrite tuner's top_5/bottom_5 labels)
                        label = 'top' if rate > 5 else ('good' if rate > 2 else 'mid')
                        cur.execute("""
                            UPDATE twitter_bot.tweets_v2
                            SET rlhf_classified = %s
                            WHERE id = %s
                              AND (rlhf_classified IS NULL
                                   OR rlhf_classified NOT IN ('top', 'top_5', 'bottom_5'))
                        """, (label, db_id))

                    self.db_conn.commit()
                    matched += 1
                    logger.info(f"📊 {twitter_id}: {impressions} views, {likes} ❤️, {rate:.1f}% eng → {label}")
                except Exception as e:
                    logger.error(f"❌ DB write failed for {twitter_id}: {e}")
                    try:
                        self.db_conn.rollback()
                    except Exception:
                        pass

                # Small delay between requests to avoid rate limiting
                time.sleep(0.5)

            logger.info(f"📊 Updated metrics for {matched}/{len(rows)} tracked tweets")
            if matched:
                refresh_runtime_rollups(self.db_conn)
                logger.info("📈 Refreshed analytics rollups")

        except Exception as e:
            logger.error(f"❌ Engagement tracking failed: {e}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass

    async def run_forever(self):
        self.connect_db()
        logger.info("🚀 Engagement Tracker started (GraphQL mode)")
        while True:
            try:
                # Run synchronous fetch_and_store in executor to avoid blocking event loop
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.fetch_and_store)
            except Exception as e:
                logger.error(f"❌ Cycle failed: {e}")
            await asyncio.sleep(1800)  # Every 30 minutes


async def main():
    tracker = EngagementTracker()
    await tracker.run_forever()


if __name__ == '__main__':
    asyncio.run(main())
