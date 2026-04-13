#!/usr/bin/env python3
"""
Style Scraper — Viral Tweet Intelligence Engine
Scrapes top-performing tweets from successful CS2 accounts.
Stores them as a rotating "style bank" that feeds into the Writer agent's prompt.
Runs every 6 hours via PM2.

Pipeline:
  1. Scrape top CS2 accounts using GraphQL guest token API
  2. Score tweets by engagement (likes + RT*2 + quotes*3)
  3. Filter: CS2-only, 40-200 chars, high engagement ratio
  4. Store in DB: style_bank table
  5. Content generator pulls top 10 recent examples as few-shot context

Target accounts:
  CS2 fan accounts that get 1k-100k likes with short, punchy tweets.
  NOT news accounts (HLTV, Dexerto). We want FAN voice.
"""

import asyncio
import logging
import os
import re
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any

from dotenv import load_dotenv
import psycopg2
from curl_cffi.requests import Session as CurlSession

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


# ─── Target Accounts (Fan Voice, High Engagement) ───────────────
# These are CS2 fan accounts that get massive engagement with short tweets.
# Updated periodically — add/remove as accounts rise/fall.
STYLE_TARGETS = {
    'Ozzny_CS2': {'style': 'dry humor, one-liners, meme energy'},
    'ThourCS2': {'style': 'fast news reaction, clean takes'},
    'CS2News_EN': {'style': 'factual but punchy, community voice'},
    'RazedEsport': {'style': 'breaking news, hype moments'},
    'BLASTPremier': {'style': 'production moments, player clips'},
    'HLTVorg': {'style': 'factual stats, match results'},
    'CScribe': {'style': 'roster intel, insider scoops'},
    'Slasher': {'style': 'breaking esports news, sharp reporting'},
    'Dust2us': {'style': 'NA CS2 news, match recaps'},
    's1mpleO': {'style': 'pro player voice, raw takes'},
}

# Twitter GraphQL — same approach as twitter_monitor.py
TWITTER_BEARER = 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA'
GRAPHQL_USER_TWEETS = 'https://twitter.com/i/api/graphql/V7H0Ap3_Hh2FyS75OCDO3Q/UserTweets'
GRAPHQL_USER_BY_SCREEN_NAME = 'https://twitter.com/i/api/graphql/G3KGOASz96M-Qu0nwmGXNg/UserByScreenName'


class StyleScraper:
    """Scrapes viral CS2 tweets for style learning"""

    def __init__(self):
        self.db_conn = None
        self._curl = CurlSession(impersonate='chrome')
        self._guest_token = None
        self._guest_token_ts = 0
        self._user_id_cache: Dict[str, str] = {}
        self._failed_lookups: Dict[str, int] = {}  # username → consecutive failure count

    def connect_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    def _ensure_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    def _refresh_guest_token(self):
        """Get a fresh guest token from Twitter"""
        now = time.time()
        if self._guest_token and (now - self._guest_token_ts) < 7000:
            return

        try:
            r = self._curl.post(
                'https://api.twitter.com/1.1/guest/activate.json',
                headers={'Authorization': f'Bearer {TWITTER_BEARER}'},
                timeout=10
            )
            if r.status_code == 200:
                self._guest_token = r.json().get('guest_token')
                self._guest_token_ts = now
                logger.info(f"🎟️  Guest token refreshed: {self._guest_token[:8]}...")
            else:
                logger.error(f"❌ Guest token failed: {r.status_code}")
        except Exception as e:
            logger.error(f"❌ Guest token error: {e}")

    def _graphql_headers(self) -> dict:
        self._refresh_guest_token()
        return {
            'Authorization': f'Bearer {TWITTER_BEARER}',
            'x-guest-token': self._guest_token or '',
            'Content-Type': 'application/json',
        }

    def _get_user_id(self, username: str) -> Optional[str]:
        """Resolve @username → numeric user ID via GraphQL"""
        if username in self._user_id_cache:
            return self._user_id_cache[username]

        variables = json.dumps({
            'screen_name': username,
            'withSafetyModeUserFields': True,
        })
        features = json.dumps({
            'hidden_profile_subscriptions_enabled': True,
            'rweb_tipjar_consumption_enabled': True,
            'responsive_web_graphql_exclude_directive_enabled': True,
            'verified_phone_label_enabled': False,
            'highlights_tweets_tab_ui_enabled': True,
            'responsive_web_twitter_article_notes_tab_enabled': True,
            'subscriptions_feature_can_gift_premium': True,
            'creator_subscriptions_tweet_preview_api_enabled': True,
            'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
            'responsive_web_graphql_timeline_navigation_enabled': True,
        })

        try:
            r = self._curl.get(
                GRAPHQL_USER_BY_SCREEN_NAME,
                params={'variables': variables, 'features': features},
                headers=self._graphql_headers(),
                timeout=10
            )
            if r.status_code != 200:
                logger.warning(f"⚠️  User lookup {username}: HTTP {r.status_code}")
                return None

            data = r.json()
            user_id = data['data']['user']['result']['rest_id']
            self._user_id_cache[username] = user_id
            return user_id
        except Exception as e:
            logger.warning(f"⚠️  User lookup {username}: {e}")
            return None

    def _fetch_user_tweets(self, user_id: str, count: int = 40) -> List[Dict]:
        """Fetch recent tweets via GraphQL"""
        variables = json.dumps({
            'userId': user_id,
            'count': count,
            'includePromotedContent': False,
            'withQuickPromoteEligibilityTweetFields': False,
            'withVoice': False,
            'withV2Timeline': True,
        })
        features = json.dumps({
            'rweb_tipjar_consumption_enabled': True,
            'responsive_web_graphql_exclude_directive_enabled': True,
            'verified_phone_label_enabled': False,
            'creator_subscriptions_tweet_preview_api_enabled': True,
            'responsive_web_graphql_timeline_navigation_enabled': True,
            'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
            'communities_web_enable_tweet_community_results_fetch': True,
            'c9s_tweet_anatomy_moderator_badge_enabled': True,
            'articles_preview_enabled': True,
            'responsive_web_edit_tweet_api_enabled': True,
            'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
            'view_counts_everywhere_api_enabled': True,
            'longform_notetweets_consumption_enabled': True,
            'responsive_web_twitter_article_tweet_consumption_enabled': True,
            'tweet_awards_web_tipping_enabled': False,
            'creator_subscriptions_quote_tweet_preview_enabled': False,
            'freedom_of_speech_not_reach_fetch_enabled': True,
            'standardized_nudges_misinfo': True,
            'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
            'rweb_video_timestamps_enabled': True,
            'longform_notetweets_rich_text_read_enabled': True,
            'longform_notetweets_inline_media_enabled': True,
            'responsive_web_enhance_cards_enabled': False,
        })

        try:
            r = self._curl.get(
                GRAPHQL_USER_TWEETS,
                params={'variables': variables, 'features': features},
                headers=self._graphql_headers(),
                timeout=15
            )
            if r.status_code != 200:
                logger.warning(f"⚠️  Tweets fetch: HTTP {r.status_code}")
                return []

            return self._parse_tweets(r.json(), user_id)
        except Exception as e:
            logger.warning(f"⚠️  Tweets fetch error: {e}")
            return []

    def _parse_tweets(self, data: dict, user_id: str) -> List[Dict]:
        """Extract tweet data from GraphQL response"""
        tweets = []
        try:
            instructions = data.get('data', {}).get('user', {}).get('result', {}).get(
                'timeline_v2', {}).get('timeline', {}).get('instructions', [])

            for instruction in instructions:
                entries = instruction.get('entries', [])
                for entry in entries:
                    content = entry.get('content', {})
                    item = content.get('itemContent', {})
                    if not item:
                        # Check for module items
                        items = content.get('items', [])
                        for sub in items:
                            item = sub.get('item', {}).get('itemContent', {})
                            tweet = self._extract_tweet_data(item)
                            if tweet:
                                tweets.append(tweet)
                        continue

                    tweet = self._extract_tweet_data(item)
                    if tweet:
                        tweets.append(tweet)
        except Exception as e:
            logger.warning(f"⚠️  Parse error: {e}")

        return tweets

    def _extract_tweet_data(self, item: dict) -> Optional[Dict]:
        """Extract engagement metrics from a single tweet item"""
        try:
            result = item.get('tweet_results', {}).get('result', {})
            if not result or result.get('__typename') == 'TweetTombstone':
                return None

            # Handle soft-deleted tweets
            if 'tweet' in result:
                result = result['tweet']

            legacy = result.get('legacy', {})
            if not legacy:
                return None

            text = legacy.get('full_text', '')
            if not text:
                return None

            # Skip retweets
            if text.startswith('RT @'):
                return None

            # Remove t.co links for cleaner text
            clean_text = re.sub(r'https?://t\.co/\S+', '', text).strip()

            tweet_id = legacy.get('id_str', '')
            likes = legacy.get('favorite_count', 0)
            retweets = legacy.get('retweet_count', 0)
            replies = legacy.get('reply_count', 0)
            quotes = legacy.get('quote_count', 0)
            bookmarks = legacy.get('bookmark_count', 0)
            views = result.get('views', {}).get('count')
            views = int(views) if views else 0

            # Extract media info
            media_entities = legacy.get('extended_entities', {}).get('media', [])
            has_image = any(m.get('type') == 'photo' for m in media_entities)
            has_video = any(m.get('type') in ('video', 'animated_gif') for m in media_entities)
            media_urls = [m.get('media_url_https', '') for m in media_entities if m.get('type') == 'photo']
            video_urls = []
            for m in media_entities:
                if m.get('type') == 'video':
                    variants = m.get('video_info', {}).get('variants', [])
                    mp4s = [v for v in variants if v.get('content_type') == 'video/mp4']
                    if mp4s:
                        best = max(mp4s, key=lambda v: v.get('bitrate', 0))
                        video_urls.append(best['url'])

            # Engagement score: weighted composite
            engagement_score = likes + (retweets * 2) + (quotes * 3) + (replies * 1.5) + (bookmarks * 2)

            created_at = legacy.get('created_at', '')

            return {
                'tweet_id': tweet_id,
                'text': clean_text,
                'full_text': text,
                'likes': likes,
                'retweets': retweets,
                'replies': replies,
                'quotes': quotes,
                'bookmarks': bookmarks,
                'views': views,
                'engagement_score': engagement_score,
                'has_image': has_image,
                'has_video': has_video,
                'media_urls': media_urls,
                'video_urls': video_urls,
                'created_at': created_at,
                'char_count': len(clean_text),
            }
        except Exception as e:
            logger.debug(f"⚠️  Tweet extract error: {e}")
            return None

    def _is_cs2_tweet(self, text: str) -> bool:
        """Quick CS2 relevance check"""
        text_lower = text.lower()
        cs2_signals = [
            'cs2', 'counter-strike', 'counter strike', 'csgo', 'cs:go',
            'hltv', 'major', 'esl', 'blast', 'iem', 'pgl', 'faceit',
            'navi', 'vitality', 'faze', 'g2', 'spirit', 'mouz', 'liquid',
            'astralis', 'fnatic', 'furia', 'heroic', 'cloud9', 'complexity',
            's1mple', 'zywoo', 'niko', 'donk', 'm0nesy', 'device', 'ropz',
            'awp', 'ak-47', 'de_', 'dust2', 'mirage', 'inferno', 'nuke',
            'overpass', 'anubis', 'ancient', 'vertigo',
            'b site', 'a site', 'ct side', 't side', 'eco round',
            'ace', 'clutch', '1v', 'defuse', 'plant',
            'valv', 'gaben', 'workshop', 'operation',
        ]
        return any(s in text_lower for s in cs2_signals)

    def _is_viral_worthy(self, tweet: Dict) -> bool:
        """Filter: is this tweet worth learning from?"""
        text = tweet['text']

        # Must be CS2 related
        if not self._is_cs2_tweet(text):
            return False

        # Character length: we want SHORT punchy tweets (40-200 chars)
        if tweet['char_count'] < 25 or tweet['char_count'] > 220:
            return False

        # Minimum engagement: at least 50 likes
        if tweet['likes'] < 50:
            return False

        # Skip tweets that are just links
        if len(text) < 15:
            return False

        # Skip generic "follow for CS2 content" type tweets
        spam_phrases = ['follow me', 'follow for', 'giveaway', 'dm me', 'subscribe',
                        'check out', 'link in bio', 'free skins', 'promo code']
        if any(p in text.lower() for p in spam_phrases):
            return False

        return True

    def scrape_account(self, username: str, style_desc: str) -> List[Dict]:
        """Scrape one account and return viral-worthy tweets"""
        # Skip accounts that have failed 3+ times in a row
        if self._failed_lookups.get(username, 0) >= 3:
            logger.debug(f"⏭️  Skipping @{username} — {self._failed_lookups[username]} consecutive failures")
            return []

        logger.info(f"📡 Scraping @{username}...")

        user_id = self._get_user_id(username)
        if not user_id:
            self._failed_lookups[username] = self._failed_lookups.get(username, 0) + 1
            logger.warning(f"⚠️  Could not resolve @{username} (fail #{self._failed_lookups[username]})")
            return []

        self._failed_lookups.pop(username, None)  # Reset on success
        raw_tweets = self._fetch_user_tweets(user_id, count=40)
        logger.info(f"  → {len(raw_tweets)} raw tweets fetched")

        viral = [t for t in raw_tweets if self._is_viral_worthy(t)]
        for t in viral:
            t['source_account'] = username
            t['source_style'] = style_desc

        logger.info(f"  → {len(viral)} viral-worthy CS2 tweets")
        return viral

    def store_tweets(self, tweets: List[Dict]):
        """Store viral tweets in the style_bank table (upsert by tweet_id)"""
        self._ensure_db()
        stored = 0

        for t in tweets:
            try:
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO twitter_bot.style_bank
                        (tweet_id, source_account, source_style, text, full_text,
                         likes, retweets, replies, quotes, bookmarks, views,
                         engagement_score, has_image, has_video, media_urls,
                         video_urls, char_count, scraped_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (tweet_id) DO UPDATE SET
                            likes = EXCLUDED.likes,
                            retweets = EXCLUDED.retweets,
                            replies = EXCLUDED.replies,
                            quotes = EXCLUDED.quotes,
                            bookmarks = EXCLUDED.bookmarks,
                            views = EXCLUDED.views,
                            engagement_score = EXCLUDED.engagement_score,
                            scraped_at = NOW()
                    """, (
                        t['tweet_id'], t['source_account'], t['source_style'],
                        t['text'], t['full_text'],
                        t['likes'], t['retweets'], t['replies'], t['quotes'],
                        t['bookmarks'], t['views'], t['engagement_score'],
                        t['has_image'], t['has_video'],
                        json.dumps(t['media_urls']), json.dumps(t['video_urls']),
                        t['char_count']
                    ))
                    self.db_conn.commit()
                    stored += 1
            except Exception as e:
                logger.warning(f"⚠️  Store error for {t['tweet_id']}: {e}")
                self.db_conn.rollback()

        logger.info(f"💾 Stored {stored}/{len(tweets)} tweets in style_bank")

    def get_style_examples(self, count: int = 10, category: str = None) -> List[str]:
        """
        Pull top-performing style examples for the Writer agent prompt.
        Called by content_generator.py at generation time.

        Returns list of tweet strings, sorted by engagement_score descending.
        """
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT text, source_account, likes, engagement_score, has_image, has_video
                    FROM twitter_bot.style_bank
                    WHERE scraped_at > NOW() - INTERVAL '14 days'
                    AND engagement_score > 100
                    ORDER BY engagement_score DESC
                    LIMIT %s
                """, (count,))

                rows = cur.fetchall()
                examples = []
                for text, account, likes, score, has_img, has_vid in rows:
                    media_tag = ""
                    if has_vid:
                        media_tag = " [had video]"
                    elif has_img:
                        media_tag = " [had image]"
                    examples.append(f'"{text}" — @{account} ({likes} likes{media_tag})')
                return examples
        except Exception as e:
            logger.warning(f"⚠️  Style bank query failed: {e}")
            return []

    def get_media_inspiration(self, count: int = 5) -> List[Dict]:
        """
        Pull tweets that went viral WITH media (images/video).
        Used by engagement_engine to learn what media formats work.
        """
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT text, source_account, likes, has_image, has_video,
                           media_urls, video_urls, engagement_score
                    FROM twitter_bot.style_bank
                    WHERE (has_image = TRUE OR has_video = TRUE)
                    AND scraped_at > NOW() - INTERVAL '14 days'
                    ORDER BY engagement_score DESC
                    LIMIT %s
                """, (count,))

                return [
                    {
                        'text': r[0], 'account': r[1], 'likes': r[2],
                        'has_image': r[3], 'has_video': r[4],
                        'media_urls': json.loads(r[5]) if r[5] else [],
                        'video_urls': json.loads(r[6]) if r[6] else [],
                        'score': r[7]
                    }
                    for r in cur.fetchall()
                ]
        except Exception as e:
            logger.warning(f"⚠️  Media inspiration query failed: {e}")
            return []

    async def run_cycle(self):
        """Scrape all target accounts"""
        self.connect_db()
        all_tweets = []

        for username, info in STYLE_TARGETS.items():
            try:
                tweets = self.scrape_account(username, info['style'])
                all_tweets.extend(tweets)
                await asyncio.sleep(3)  # Rate limit: 3s between accounts
            except Exception as e:
                logger.error(f"❌ Failed to scrape @{username}: {e}")

        if all_tweets:
            self.store_tweets(all_tweets)

        # Log insight summary
        if all_tweets:
            avg_likes = sum(t['likes'] for t in all_tweets) / len(all_tweets)
            with_media = sum(1 for t in all_tweets if t['has_image'] or t['has_video'])
            logger.info(
                f"📊 Style bank update: {len(all_tweets)} tweets, "
                f"avg {avg_likes:.0f} likes, {with_media} with media "
                f"({with_media/len(all_tweets)*100:.0f}%)"
            )

    async def run_forever(self):
        """Main loop — runs every 6 hours"""
        logger.info("🚀 Style Scraper started — learning from the best CS2 accounts")
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"❌ Cycle failed: {e}")
            await asyncio.sleep(6 * 3600)  # 6 hours


# Singleton
_style_scraper = None

def get_style_scraper() -> StyleScraper:
    global _style_scraper
    if _style_scraper is None:
        _style_scraper = StyleScraper()
    return _style_scraper


if __name__ == '__main__':
    asyncio.run(StyleScraper().run_forever())
