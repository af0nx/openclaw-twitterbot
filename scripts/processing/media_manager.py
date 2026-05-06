#!/usr/bin/env python3
"""
Media Manager - Twitter Bot Pipeline V2
Handles image extraction from source articles, HLTV player bodyshots,
team logos, and meme/reaction image library.
Downloads images, stores locally, provides Tweepy v1.1 media upload.

Player Image Pipeline:
  1. Match analysis detects player names in tweet/event
  2. Looks up HLTV player ID from built-in mapping (50+ top pros)
  3. Scrapes HLTV player page for bodyshot image URL
  4. Downloads with Referer header (HLTV CDN requires it)
  5. Uploads via Tweepy v1.1 → returns media_id for v2 posting
"""

import html as html_module
import logging
import os
import re
import hashlib
import html
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, List, Dict
from urllib.parse import quote_plus, unquote_plus

from curl_cffi.requests import Session as CurlSession
from PIL import Image
import tweepy

from dotenv import load_dotenv
load_dotenv('/dev/shm/.env')

from utils.twitter_accounts import ACCOUNT_BUCKETS, get_account_credentials

logger = logging.getLogger(__name__)

REACTIONS_DIR = Path(__file__).parent.parent.parent / 'assets' / 'cs2-reactions'
MEDIA_CACHE = Path(tempfile.gettempdir()) / 'cs2_media_cache'
MEDIA_CACHE.mkdir(exist_ok=True)

# ─── HLTV Player ID Database ──────────────────────────────────────────
# Top 50+ CS2 pros. Key = lowercase alias, Value = HLTV player ID.
# Used to fetch bodyshot images from HLTV player profiles.
HLTV_PLAYER_IDS: Dict[str, int] = {
    # Spirit
    'donk': 22078, 'chopper': 13666, 'magixx': 18988, 'zont1x': 19907, 'sh1ro': 18899,
    # NaVi
    's1mple': 7998, 'electronic': 8918, 'bit': 18987, 'jl': 19419, 'im': 22627,
    'wonderful': 20908, 'b1ad3': 484, 'aleksib': 14317,
    # Vitality
    'zywoo': 11893, 'spinx': 17547, 'flamez': 16820, 'ropz': 11816,
    'mezii': 15542, 'apex': 7322,
    # FaZe
    'rain': 8183, 'karrigan': 429, 'broky': 18053, 'frozen': 14116, 'ropz': 11816,
    # G2
    'niko': 3741, 'hunter': 18457, 'nexa': 8960, 'm0nesy': 20127, 'malbs': 18924,
    'stewie2k': 8797, 'hooxi': 6956,
    # MOUZ
    'brollan': 14065, 'siuhy': 18155, 'torzsi': 16739, 'jimpphat': 20342,
    'xertion': 20916,
    # Liquid
    'elige': 8738, 'naf': 8520, 'yekindar': 13915, 'skullz': 18707,
    # Astralis
    'device': 7592, 'gla1ve': 7412, 'blamef': 15165, 'stavn': 16243,
    # FURIA
    'kscerato': 15631, 'yuurih': 17703, 'art': 17972, 'fallen': 2023,
    # Heroic
    'cadian': 7964, 'degster': 18981,
    # Eternal Fire
    'calyx': 9632, 'wicadia': 21130, 'xantares': 7938,
    # Complexity
    'elige': 8738, 'hallzerk': 16712,
    # Others
    'twistzz': 10394, 'hobbit': 6475, 'ax1le': 18233,
    'perfecto': 16947, 'magisk': 9032, 'dupreeh': 7398,
    'olofmeister': 885, 'guardian': 2757, 'coldzera': 9216,
    'kennys': 7167, 'shox': 1225, 'nawwk': 15077,
}

# Common name aliases → canonical HLTV name
PLAYER_ALIASES: Dict[str, str] = {
    'b1t': 'bit', 'monesy': 'm0nesy', 'xertioN': 'xertion',
    'blameF': 'blamef', 'cadiaN': 'cadian', 'ZywOo': 'zywoo',
    'NiKo': 'niko', 'EliGE': 'elige', 'NAF': 'naf',
    'ropz': 'ropz', 'broky': 'broky', 'kscerato': 'kscerato',
    'FalleN': 'fallen', 'stavn': 'stavn',
    'sh1ro': 'sh1ro', 'shiro': 'sh1ro',
}


class MediaManager:
    """Extract, download, and upload media for tweets — including player bodyshots"""

    # Mood → reaction images (filenames in assets/cs2-reactions/)
    MOOD_REACTIONS = {
        'hype': ['hype1.jpg', 'hype2.jpg'],
        'rip': ['rip1.jpg', 'rip2.jpg'],
        'shock': ['shock1.jpg', 'shock2.jpg'],
        'clutch': ['clutch1.jpg'],
        'update': ['update1.jpg'],
    }

    # Known HLTV team IDs for fast lookup (avoids search)
    HLTV_TEAM_IDS: Dict[str, int] = {
        'natus vincere': 4608, 'navi': 4608, 'vitality': 9565, 'faze': 6667,
        'faze clan': 6667, 'g2': 5995, 'g2 esports': 5995, 'spirit': 7020,
        'team spirit': 7020, 'mouz': 4494, 'mousesports': 4494, 'liquid': 5973,
        'team liquid': 5973, 'heroic': 7175, 'fnatic': 4991, 'furia': 8297,
        'astralis': 6665, 'big': 7532, 'eternal fire': 11251, 'complexity': 5005,
        'cloud9': 5752, 'virtus.pro': 5378, 'monte': 11811, 'sinners': 10577,
        'imperial': 9455, '3dmax': 10150, 'saw': 10567, 'pain': 4773,
        'apeks': 10495, 'gamerlegion': 11105, 'the mongolz': 7608,
        'lynn vision': 10447, 'betboom': 11518,
        'loud': 11633, 'wildcard': 12010, 'nrg': 4966, 'eyeballers': 7718,
        'ence': 4869, 'falcons': 12229, 'fut': 12520, 'rare atom': 12374,
        'mibr': 9215,
    }

    def __init__(self):
        self._curl = CurlSession(impersonate='chrome')
        self._apis = self._init_v1_apis()
        self._api = self._apis.get('main') or next(iter(self._apis.values()), None)
        # Cache: player_name → bodyshot URL (avoid re-scraping)
        self._bodyshot_cache: Dict[str, str] = {}
        # Cache: team_name → list of (player_id, player_name)
        self._team_roster_cache: Dict[str, List[tuple]] = {}
        self._CACHE_MAX = 200  # Evict when exceeding this

    def _init_v1_apis(self) -> Dict[str, tweepy.API]:
        """Initialize Tweepy v1.1 APIs for media uploads across account buckets."""
        apis: Dict[str, tweepy.API] = {}
        for bucket in ACCOUNT_BUCKETS:
            try:
                creds = get_account_credentials(bucket)
                if not all([creds['api_key'], creds['api_secret'], creds['access_token'], creds['access_secret']]):
                    continue
                auth = tweepy.OAuth1UserHandler(
                    creds['api_key'],
                    creds['api_secret'],
                    creds['access_token'],
                    creds['access_secret'],
                )
                apis[bucket] = tweepy.API(auth)
            except Exception as e:
                logger.warning(f"⚠️  v1.1 API init failed for {bucket} (media upload disabled): {e}")
        return apis

    def _get_api(self, account_bucket: str = 'main') -> Optional[tweepy.API]:
        return self._apis.get(account_bucket) or self._apis.get('main') or self._api

    # ─── Player Image Methods ─────────────────────────────────────

    # Short names that need word-boundary matching to avoid false positives
    _SHORT_NAMES = {'im', 'bit', 'art', 'naf', 'jl', 'ax1le'}

    def find_players_in_text(self, text: str) -> List[str]:
        """Find CS2 player names mentioned in text, return list of canonical names.
        Uses word-boundary matching for short names to avoid false positives
        (e.g. 'im' matching inside 'time', 'bit' inside 'ambitious').
        Returns players in order of first appearance in text (not dict order).
        """
        if not text:
            return []
        text_lower = text.lower()
        # Collect (position, canonical_name) tuples
        positions: List[tuple] = []
        seen = set()
        for player_name in HLTV_PLAYER_IDS:
            if player_name in seen:
                continue
            if player_name in self._SHORT_NAMES:
                m = re.search(r'\b' + re.escape(player_name) + r'\b', text_lower)
                if m:
                    positions.append((m.start(), player_name))
                    seen.add(player_name)
            else:
                idx = text_lower.find(player_name)
                if idx >= 0:
                    positions.append((idx, player_name))
                    seen.add(player_name)
        # Also check aliases
        for alias, canonical in PLAYER_ALIASES.items():
            alias_lower = alias.lower()
            if canonical not in seen:
                if len(alias_lower) <= 3:
                    m = re.search(r'\b' + re.escape(alias_lower) + r'\b', text_lower)
                    if m:
                        positions.append((m.start(), canonical))
                        seen.add(canonical)
                elif alias_lower in text_lower:
                    idx = text_lower.find(alias_lower)
                    positions.append((idx, canonical))
                    seen.add(canonical)
        # Sort by position in text so first-mentioned player is first
        positions.sort(key=lambda x: x[0])
        return [name for _, name in positions]

    def get_player_bodyshot_url(self, player_name: str) -> Optional[str]:
        """Fetch HLTV player page and extract bodyshot image URL"""
        canonical = PLAYER_ALIASES.get(player_name, player_name).lower()

        # Check cache first
        if canonical in self._bodyshot_cache:
            return self._bodyshot_cache[canonical]

        player_id = HLTV_PLAYER_IDS.get(canonical)
        if not player_id:
            return None

        try:
            url = f"https://www.hltv.org/player/{player_id}/{canonical}"
            r = self._curl.get(url, timeout=12, headers={
                'Referer': 'https://www.hltv.org/',
                'Accept': 'text/html',
            })
            if r.status_code != 200:
                logger.warning(f"⚠️  HLTV player page {url}: HTTP {r.status_code}")
                return None

            # Extract bodyshot URL (first one is the big hero image)
            bodyshots = re.findall(
                r'(https://img-cdn\.hltv\.org/playerbodyshot/[^\s"<>&]+(?:&amp;[^\s"<>&]+)*)',
                r.text
            )
            if not bodyshots:
                logger.warning(f"⚠️  No bodyshot found for {canonical}")
                return None

            # Clean HTML entities and get the best resolution
            img_url = bodyshots[0].replace('&amp;', '&')

            # Cache it (with eviction)
            if len(self._bodyshot_cache) > self._CACHE_MAX:
                self._bodyshot_cache.clear()
            self._bodyshot_cache[canonical] = img_url
            logger.info(f"📸 Found bodyshot for {canonical}")
            return img_url

        except Exception as e:
            logger.warning(f"⚠️  Failed to fetch bodyshot for {canonical}: {e}")
            return None

    def get_player_image(self, player_name: str, account_bucket: str = 'main') -> Optional[str]:
        """Get player bodyshot: fetch URL → download → upload → return media_id"""
        img_url = self.get_player_bodyshot_url(player_name)
        if not img_url:
            return None

        local_path = self.download_image(img_url, referer='https://www.hltv.org/')
        if not local_path:
            return None

        return self.upload_media(local_path, account_bucket=account_bucket)

    def get_team_player_image(self, team_name: str, account_bucket: str = 'main') -> Optional[str]:
        """
        Dynamically fetch a player bodyshot from any HLTV team page.
        Looks up team by name → scrapes roster → gets first player's bodyshot.
        Returns media_id or None.
        """
        team_lower = team_name.lower().strip()

        # Check roster cache
        if team_lower in self._team_roster_cache:
            roster = self._team_roster_cache[team_lower]
        else:
            # Get team HLTV ID
            team_id = self.HLTV_TEAM_IDS.get(team_lower)
            if not team_id:
                # Try partial match
                for known, tid in self.HLTV_TEAM_IDS.items():
                    if known in team_lower or team_lower in known:
                        team_id = tid
                        break
            if not team_id:
                logger.debug(f"No HLTV team ID for '{team_name}'")
                return None

            try:
                slug = team_lower.replace(' ', '-').replace('.', '')
                url = f"https://www.hltv.org/team/{team_id}/{slug}"
                r = self._curl.get(url, timeout=12, headers={
                    'Referer': 'https://www.hltv.org/',
                    'Accept': 'text/html',
                })
                if r.status_code != 200:
                    logger.warning(f"⚠️  HLTV team page {url}: HTTP {r.status_code}")
                    return None

                # Extract player links from team page
                player_links = re.findall(r'/player/(\d+)/(\w+)', r.text)
                seen = set()
                roster = []
                for pid, pname in player_links:
                    if pid not in seen:
                        seen.add(pid)
                        roster.append((int(pid), pname))

                if len(self._team_roster_cache) > self._CACHE_MAX:
                    self._team_roster_cache.clear()
                self._team_roster_cache[team_lower] = roster
                logger.info(f"📋 {team_name} roster: {[p[1] for p in roster[:5]]}")

            except Exception as e:
                logger.warning(f"⚠️  Failed to fetch team roster for {team_name}: {e}")
                return None

        # Try each roster player until we get a bodyshot
        for pid, pname in roster[:3]:
            try:
                r2 = self._curl.get(
                    f'https://www.hltv.org/player/{pid}/{pname}',
                    timeout=12,
                    headers={'Referer': 'https://www.hltv.org/', 'Accept': 'text/html'}
                )
                if r2.status_code != 200:
                    continue

                bodyshots = re.findall(
                    r'(https://img-cdn\.hltv\.org/playerbodyshot/[^\s"<>&]+(?:&amp;[^\s"<>&]+)*)',
                    r2.text
                )
                if not bodyshots:
                    continue

                img_url = bodyshots[0].replace('&amp;', '&')
                local = self.download_image(img_url, referer='https://www.hltv.org/')
                if local:
                    media_id = self.upload_media(local, account_bucket=account_bucket)
                    if media_id:
                        logger.info(f"📸 Team player image: {pname} ({team_name})")
                        return media_id
            except Exception:
                continue

        return None

    def get_best_player_image_for_match(self, event: dict, tweet_text: str = '', account_bucket: str = 'main') -> Optional[str]:
        """
        Find the most relevant player to show for a match tweet.
        Priority: players mentioned in tweet > winner's star player > any player from match.
        Returns media_id or None.
        """
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            return None

        # 1. Check for players mentioned in the tweet text
        players_in_tweet = self.find_players_in_text(tweet_text)
        if players_in_tweet:
            media_id = self.get_player_image(players_in_tweet[0], account_bucket=account_bucket)
            if media_id:
                logger.info(f"📸 Player image (from tweet): {players_in_tweet[0]}")
                return media_id

        # 2. Check for players from match metadata
        all_text = (
            (event.get('headline') or '') + ' ' +
            (event.get('content') or '') + ' ' +
            str(metadata)
        )
        players_in_event = self.find_players_in_text(all_text)
        if players_in_event:
            media_id = self.get_player_image(players_in_event[0], account_bucket=account_bucket)
            if media_id:
                logger.info(f"📸 Player image (from event): {players_in_event[0]}")
                return media_id

        # 3. Try winner's team star player (known mapping)
        winner = (metadata.get('winner') or '').lower()
        team_stars = {
            'natus vincere': 'jl', 'navi': 'jl',
            'vitality': 'zywoo', 'faze': 'broky', 'faze clan': 'broky',
            'g2': 'niko', 'g2 esports': 'niko',
            'spirit': 'donk', 'team spirit': 'donk',
            'mouz': 'brollan', 'mousesports': 'brollan',
            'liquid': 'elige', 'team liquid': 'elige',
            'furia': 'kscerato', 'heroic': 'stavn',
            'astralis': 'device', 'fnatic': 'brollan',
            'eternal fire': 'xantares', 'complexity': 'elige',
            'cloud9': 'ax1le',
        }
        star = team_stars.get(winner)
        if star:
            media_id = self.get_player_image(star, account_bucket=account_bucket)
            if media_id:
                logger.info(f"📸 Player image (team star): {star} ({winner})")
                return media_id

        # 4. Dynamic HLTV team roster lookup — works for ANY team
        # Try winner first, then team1, then team2
        for team_key in ['winner', 'team1', 'team2']:
            team = metadata.get(team_key)
            if team:
                media_id = self.get_team_player_image(team, account_bucket=account_bucket)
                if media_id:
                    return media_id

        return None

    # ─── Google Images Search ─────────────────────────────────────

    _IMG_BLOCKED_DOMAINS = {
        'gstatic.com', 'google.com', 'googleapis.com', 'ggpht.com',
        'ytimg.com', 'facebook.com', 'instagram.com', 'tiktok.com',
        'pinterest.com', 'wikimedia.org',
    }

    _IMAGE_QUERY_STOPWORDS = {
        'cs2', 'csgo', 'counter', 'strike', 'esports', 'esport', 'photo',
        'photos', 'players', 'player', 'stage', 'event', 'events', 'team',
        'teams', 'match', 'matches', 'gaming', 'game', 'large', 'preview',
        'the', 'and', 'after', 'with', 'from', 'into', 'will', 'for',
        'bench', 'benched', 'release', 'released', 'roster', 'following',
        'internal', 'issues', 'instability', 'host', 'hosted', 'season',
        'make', 'made', 'top', 'topped', 'announce', 'announced',
    }

    @staticmethod
    def _normalize_image_match_text(text: str) -> str:
        return re.sub(r'[^a-z0-9]+', ' ', str(text or '').lower()).strip()

    @classmethod
    def _required_image_subject_groups(cls, query: str) -> List[tuple]:
        """Return subject alternatives that must appear in a candidate URL.

        Search engines often return plausible-looking generic CS2 photos. For
        main-feed news, a distinctive team/player/event in the query must be
        visible in the image URL, otherwise the image is too likely wrong.
        """
        q = cls._normalize_image_match_text(query)
        rules = [
            (('9ine', 'bnox'), ('9ine', 'bnox')),
            (('aether',), ('aether',)),
            (('fisher', 'playvs'), ('fisher', 'playvs')),
            (('m80',), ('m80', 'thunderpick')),
            (('voca', 'wildcard'), ('voca', 'wildcard', 'esl challenger', 'challenger')),
            (('bc game masters', 'bcgame'), ('bc game masters', 'bcgame', 'bc game')),
            (('pgl astana',), ('pgl astana', 'astana')),
            (('blast rivals',), ('blast rivals', 'blast')),
            (('faze',), ('faze', 'blast rivals')),
            (('esl pro league', 'epl'), ('esl pro league', 'esl proleague', 'epl')),
        ]
        required = []
        for triggers, alternatives in rules:
            if triggers == ('pgl astana',) and 'ahead of pgl astana' in q:
                continue
            if any(trigger in q for trigger in triggers):
                required.append(alternatives)
        return required

    @classmethod
    def _url_matches_required_subjects(cls, url: str, query: str) -> bool:
        groups = cls._required_image_subject_groups(query)
        if not groups:
            return True
        url_text = cls._normalize_image_match_text(url)
        return all(any(alt in url_text for alt in group) for group in groups)

    @staticmethod
    def _season_numbers(text: str) -> set:
        raw = unquote_plus(str(text or '').lower())
        seasons = set()
        for match in re.finditer(r'season[\s_-]*(\d{1,2})(?!\d)', raw):
            seasons.add(match.group(1))
        for match in re.finditer(r'(?<![a-z0-9])s[\s_-]*(\d{1,2})(?!\d)', raw):
            seasons.add(match.group(1))
        for match in re.finditer(r'(?<![a-z0-9])epl[\s_-]*(\d{1,2})(?!\d)', raw):
            seasons.add(match.group(1))
        return seasons

    @classmethod
    def _has_conflicting_season(cls, url: str, query: str) -> bool:
        desired = cls._season_numbers(query)
        if not desired:
            return False
        found = cls._season_numbers(url)
        return bool(found and any(season not in desired for season in found))

    @classmethod
    def _image_url_relevance(cls, url: str, query: str) -> int:
        url_lower = re.sub(r'[^a-z0-9]+', ' ', url.lower())
        tokens = [
            token for token in re.findall(r'[a-z0-9]+', query.lower())
            if len(token) >= 3 and token not in cls._IMAGE_QUERY_STOPWORDS
        ]
        score = 0
        for token in tokens:
            if token in url_lower:
                score += 3
        # Exact important event/org fragments matter more than generic terms.
        compact_url = url.lower()
        compact_query = query.lower().replace(' ', '-')
        for fragment in ('bc-game-masters', 'thunderpick', 'challenger', '9ine', 'bnox',
                         'pgl-astana', 'blast-rivals', 'vrs', 'esl-pro-league',
                         'faze', 'furia', 'vitality', 'aether'):
            if fragment in compact_query and fragment in compact_url:
                score += 5
        return score

    def search_google_images(self, query: str, num_results: int = 5) -> List[str]:
        """Search Bing Images and return full-size image URLs.
        
        Uses Bing because Google blocks server-side scrapers (429).
        Bing returns high-quality results from dexerto, hltv, dust2.us, etc.
        """
        try:
            search_url = f"https://www.bing.com/images/search?q={quote_plus(query)}&form=HDRSC3&first=1"
            r = self._curl.get(search_url, timeout=15, headers={
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'en-US,en;q=0.9',
            })
            if r.status_code != 200:
                logger.warning(f"⚠️  Bing Images: HTTP {r.status_code}")
                return []

            # Bing HTML-entity-encodes JSON data — unescape first
            text = html_module.unescape(r.text)
            raw_urls = re.findall(r'"murl":"(https?://[^"]+)"', text)

            # Prefer esports domains
            _GOOD_DOMAINS = {
                'hltv.org', 'dexerto.com', 'dust2.us', 'dotesports.com',
                'gamearena.gg', 'esports.gg', 'win.gg', 'cs2pulse.com',
                'vitality.gg', 'esportsinsider.com', 'thunderpick.io',
                'pro.eslgaming.com',
            }
            _BAD_DOMAINS = {
                'bing.com', 'microsoft.com', 'facebook.com', 'instagram.com',
                'pinterest.com', 'tiktok.com', 'wikimedia.org', 'reddit.com',
                'bo3.gg', 'liquipedia.net',
                'url2png.com', 'thumbnail.ws', 'screenshotlayer.com',
                'thum.io', 'image.thum.io', 'api.microlink.io',
                'prosettings.net', 'csgosettings.com',
            }
            # URL path substrings that indicate settings/config pages, not player photos
            _BAD_PATH_KEYWORDS = (
                'settings', 'crosshair', 'sensitivity', 'config',
                'setup', 'keybind', 'resolution', 'viewmodel',
                'monitor', 'mouse-', 'keyboard-', 'gear',
                'logo', 'icon', 'lightmode', 'darkmode', 'commons/images',
                'esports-world-cup-2024', 'top-cs2-esports-events',
                'biggest-tournaments', 'explaining-cs2s-upper-level',
                'vrs-shapes-the-pro-ecosystem', 'cs2-rostermania-hub',
                'all-cs2-ranks', 'biggest-cs2-developments',
                'counter-strike-2-2-968x544',
            )

            seen = set()
            good = []
            okay = []
            for url in raw_urls:
                if url in seen:
                    continue
                seen.add(url)
                if any(d in url for d in _BAD_DOMAINS):
                    continue
                if len(url) > 500:
                    continue
                url_lower = url.lower()
                if any(kw in url_lower for kw in _BAD_PATH_KEYWORDS):
                    logger.debug(f"🚫 Skipping settings/config image: {url[:80]}")
                    continue
                if self._has_conflicting_season(url, query):
                    logger.debug(f"🚫 Skipping wrong-season image for '{query[:50]}': {url[:100]}")
                    continue
                if not self._url_matches_required_subjects(url, query):
                    logger.debug(f"🚫 Skipping wrong-subject image for '{query[:50]}': {url[:100]}")
                    continue
                relevance = self._image_url_relevance(url, query)
                # Prioritize esports sites
                if any(d in url for d in _GOOD_DOMAINS):
                    good.append((relevance, url))
                else:
                    okay.append((relevance, url))

            ranked = sorted(good, key=lambda item: item[0], reverse=True) + sorted(
                okay,
                key=lambda item: item[0],
                reverse=True,
            )
            # If the query has distinctive tokens, a zero-relevance URL is often
            # a wrong-team stock photo. Keep it only when no better candidate exists.
            relevant = [url for score, url in ranked if score > 0]
            results = (relevant or [url for _, url in ranked])[:num_results]
            if results:
                logger.info(f"🔍 Image search: {len(results)} results for '{query[:40]}'")
            return results
        except Exception as e:
            logger.warning(f"⚠️  Image search failed: {e}")
            return []

    @staticmethod
    def _extract_image_context(tweet_text: str) -> str:
        """Pull contextual keywords from tweet text to build a better image query.
        
        Maps emotional/event cues to search-friendly terms so we get
        relevant photos (trophy lifts, celebrations) instead of settings pages.
        """
        text_lower = tweet_text.lower()
        # Ordered: first match wins
        _CONTEXT_CUES = [
            # Major / trophy moments
            (['major', 'trophy', 'champion', 'grand final'], 'major champion trophy'),
            (['winning', 'won', 'victory', 'celebrate', 'celebration'], 'winning celebration'),
            (['crying', 'cry', 'tears', 'emotional', 'legend'], 'emotional moment'),
            (['mvp', 'award'], 'mvp award ceremony'),
            # Match context
            (['ace', 'clutch', '1v'], 'clutch highlight'),
            (['transfer', 'signing', 'joins', 'joined', 'roster'], 'roster announcement'),
            (['eliminated', 'knocked out', 'upset'], 'upset reaction'),
            (['interview', 'said', 'says', 'quote'], 'interview'),
        ]
        for keywords, context in _CONTEXT_CUES:
            if any(kw in text_lower for kw in keywords):
                return context
        return ''

    # Team names that are common English words — require ALL-CAPS in original text
    _AMBIGUOUS_TEAMS = {'big', 'saw', 'pain', 'imperial', 'loud', 'wildcard', 'rare atom'}

    _NO_EXTERNAL_MEDIA_CATEGORIES = {
        'engagement_poll',
        'engagement_conversation',
        'engagement_take',
        'engagement_recycle',
        'engagement_milestone',
        'vip_engagement',
        'community_disagreement',
    }

    _SUBJECT_REQUIRED_CATEGORIES = {
        'cs2',
        'cs2_update',
        'match_highlight',
        'analysis',
        'roster_change',
        'match_result',
        'match_preview',
    }

    @staticmethod
    def _metadata_subject_text(metadata: dict) -> str:
        if not isinstance(metadata, dict):
            return ''

        parts = []
        for key in (
            'team1', 'team2', 'team_a', 'team_b', 'pick', 'opponent',
            'winner', 'loser', 'player', 'player_name', 'mvp',
        ):
            value = metadata.get(key)
            if value:
                parts.append(str(value))

        teams = metadata.get('teams')
        if isinstance(teams, list):
            parts.extend(str(team) for team in teams if team)

        match_context = metadata.get('match_context')
        if isinstance(match_context, dict):
            for key in ('mvp', 'team1', 'team2', 'winner', 'loser'):
                value = match_context.get(key)
                if value:
                    parts.append(str(value))

        return ' '.join(parts)

    def _media_subjects(
        self,
        event: dict,
        tweet_text: str = '',
        include_event_text: bool = True,
    ) -> Dict[str, List[str]]:
        metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
        event_parts = []
        if include_event_text:
            event_parts = [event.get('headline') or '', event.get('content') or '']

        subject_text = ' '.join(
            part for part in (
                tweet_text or '',
                self._metadata_subject_text(metadata),
                *event_parts,
            )
            if part
        )
        return {
            'players': self.find_players_in_text(subject_text),
            'teams': self._find_teams_in_text(subject_text),
        }

    def _allows_external_media(self, event: dict, tweet_text: str = '') -> bool:
        """Fail closed on media matching.

        Article OG images, social-card images, and image-search results are often
        generic or stale. Only allow them when the tweet/event has an explicit
        CS2 player/team/match subject. Engagement and VIP replies stay text-only
        unless the scheduler attaches owned media before this point.
        """
        category = event.get('category', '')
        source = event.get('source', '')
        metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}

        if metadata.get('media_path'):
            return True

        if source in ('twitter', 'engagement_engine'):
            logger.info("⏭️ External media disabled for %s/%s", source, category)
            return False

        if category in self._NO_EXTERNAL_MEDIA_CATEGORIES:
            logger.info("⏭️ External media disabled for %s tweets", category)
            return False

        source_url = event.get('source_url') or metadata.get('source_url') or ''
        if source in ('hltv', 'dust2us') and source_url:
            return True

        subjects = self._media_subjects(event, tweet_text, include_event_text=True)
        has_subject = bool(subjects['players'] or subjects['teams'])

        if category in self._SUBJECT_REQUIRED_CATEGORIES and not has_subject:
            logger.info(
                "⏭️ No concrete player/team subject for %s media — posting text-only",
                category or 'unknown',
            )
            return False

        return has_subject or category == 'match_prediction'

    def _find_teams_in_text(self, text: str) -> List[str]:
        """Find known CS2 team names in text, ordered by first appearance."""
        if not text:
            return []
        text_lower = text.lower()
        positions = []
        seen = set()
        for team_name in self.HLTV_TEAM_IDS:
            if team_name in seen:
                continue
            pattern = r'\b' + re.escape(team_name) + r'\b'
            m = re.search(pattern, text_lower)
            if m and team_name not in seen:
                # For ambiguous names (common English words), require ALL-CAPS
                # in the original text to confirm it's a team reference
                if team_name in self._AMBIGUOUS_TEAMS:
                    original_span = text[m.start():m.end()]
                    if original_span != original_span.upper():
                        continue  # "Big changes" → skip, "BIG destroyed FaZe" → keep
                positions.append((m.start(), team_name))
                seen.add(team_name)
        positions.sort(key=lambda x: x[0])
        return [name for _, name in positions]

    def _build_image_search_query(self, event: dict, tweet_text: str = '') -> str:
        """Build the best Google Images search query for an event.
        
        Priority: players in tweet > teams in tweet > players in content > metadata teams > headline.
        This prevents source-article players from overriding the player the tweet is about.
        """
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            metadata = {}
        team1 = metadata.get('team1', '')
        team2 = metadata.get('team2', '')
        event_name = metadata.get('event', '')
        category = event.get('category', '')

        # 1. Check tweet text FIRST for players — this is what the tweet is about
        if tweet_text:
            tweet_players = self.find_players_in_text(tweet_text)
            if tweet_players:
                player = tweet_players[0]
                context = self._extract_image_context(tweet_text)
                if context:
                    q = f"{player} cs2 {context} esports photo"
                else:
                    q = f"{player} cs2 esports player photo 2026"
                logger.debug(f"🔍 Image search: player '{player}' from tweet text")
                return q

            # 1b. Check tweet text for team names
            tweet_teams = self._find_teams_in_text(tweet_text)
            if tweet_teams:
                team = tweet_teams[0]
                text_lower = tweet_text.lower()
                is_vs_matchup = bool(re.search(r'\bvs\.?\b|\bversus\b|\bface\b|\bfacing\b|\bplay(?:s|ing)?\s+against\b', text_lower))

                if len(tweet_teams) >= 2 and is_vs_matchup:
                    # Head-to-head matchup — use "team1 vs team2"
                    q = f"{tweet_teams[0]} vs {tweet_teams[1]} cs2 esports match photo"
                    if event_name:
                        q += f" {event_name}"
                    logger.debug(f"🔍 Image search: teams matchup from tweet text: {q}")
                    return q
                elif len(tweet_teams) >= 2:
                    # Multiple teams mentioned but NOT a vs matchup (e.g. "make the playoffs")
                    # Search for team photos rather than a vs image
                    context = self._extract_image_context(tweet_text)
                    team_names = ' '.join(tweet_teams[:3])
                    q = f"{team_names} cs2 {context or 'team esports players photo 2026'}"
                    if event_name:
                        q += f" {event_name}"
                    logger.debug(f"🔍 Image search: multi-team (non-vs) from tweet text: {q}")
                    return q
                else:
                    context = self._extract_image_context(tweet_text)
                    q = f"{team} cs2 {context or 'team esports players photo 2026'}"
                    logger.debug(f"🔍 Image search: team '{team}' from tweet text")
                    return q

        # 2. Fall back to headline + content only when no generated tweet text
        # exists yet. Once text exists, media must anchor to what users can read,
        # not to unrelated names buried in source article body text.
        fallback_text = (
            (event.get('headline') or '') + ' ' +
            (event.get('content') or '')
        )
        fallback_players = self.find_players_in_text(fallback_text) if not tweet_text else []
        if fallback_players:
            logger.debug(f"🔍 Image search: player '{fallback_players[0]}' from event content (not in tweet)")
            return f"{fallback_players[0]} cs2 esports player photo 2026"

        # 3. Team-based query from metadata
        if team1 and team2:
            q = f"{team1} vs {team2} cs2 esports match photo"
            if event_name:
                q += f" {event_name}"
            return q.strip()
        elif team1:
            return f"{team1} cs2 team esports players photo 2026"

        source = event.get('source', '')
        source_url = event.get('source_url') or metadata.get('source_url') or ''
        headline = str(event.get('headline') or '').strip()
        if source in ('hltv', 'dust2us') and source_url and headline:
            query = re.sub(r'[^A-Za-z0-9 ]+', ' ', headline)
            query = re.sub(r'\s+', ' ', query).strip()
            if query:
                return f"{query} CS2 esports event photo players stage"

        # No useful player/team keywords — return empty to skip image search.
        # Proper-noun headline searches are intentionally disabled because they
        # produced stale venue, tournament, and generic CS2 images.
        logger.info("⏭️ No identifiable player/team for image search — skipping")
        return ''

    def get_google_image(self, event: dict, tweet_text: str = '', account_bucket: str = 'main') -> Optional[str]:
        """Search Google Images for a CS2 photo, download, upload \u2192 media_id"""
        query = self._build_image_search_query(event, tweet_text)
        if not query or len(query) < 5:
            logger.info("⏭️ Image query too vague — skipping Google Images to avoid random photos")
            return None

        urls = self.search_google_images(query)
        for url in urls[:3]:
            try:
                local_path = self.download_image(url)
                if local_path:
                    media_id = self.upload_media(local_path, account_bucket=account_bucket)
                    if media_id:
                        event['_external_media_preview_path'] = local_path
                        logger.info(f"\U0001f4f8 Google image for: {query[:40]}")
                        return media_id
            except Exception as e:
                logger.debug(f"Google image attempt failed: {e}")
                continue
        return None

    def fetch_article_page(self, url: str) -> Dict[str, Optional[str]]:
        """Fetch an article page and extract OG image + article text.

        Returns {'og_image': url_or_none, 'text': article_text_or_none}
        """
        result: Dict[str, Optional[str]] = {'og_image': None, 'text': None}
        if not url:
            return result
        try:
            r = self._curl.get(url, timeout=15, headers={
                'Accept': 'text/html',
                'Referer': 'https://www.google.com/',
            })
            if r.status_code != 200:
                return result

            page = r.text

            # Extract OG image
            og_match = re.search(
                r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                page, re.IGNORECASE
            )
            if not og_match:
                og_match = re.search(
                    r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:image["\']',
                    page, re.IGNORECASE
                )
            if og_match:
                result['og_image'] = html.unescape(og_match.group(1))

            # Extract article text from <p> tags
            clean = re.sub(r'<script[^>]*>.*?</script>', '', page, flags=re.DOTALL)
            clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.DOTALL)
            clean = re.sub(r'<nav[^>]*>.*?</nav>', '', clean, flags=re.DOTALL)
            clean = re.sub(r'<header[^>]*>.*?</header>', '', clean, flags=re.DOTALL)
            clean = re.sub(r'<footer[^>]*>.*?</footer>', '', clean, flags=re.DOTALL)
            paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', clean, re.DOTALL)
            clean_paras = []
            for p in paragraphs:
                text = re.sub(r'<[^>]+>', '', p).strip()
                text = html_module.unescape(text)
                # 80+ chars filters out nav items, captions, etc.
                if len(text) > 80:
                    clean_paras.append(text)

            if clean_paras:
                result['text'] = '\n'.join(clean_paras[:10])[:1500]

            return result
        except Exception as e:
            logger.warning(f"\u26a0\ufe0f  Article page fetch failed: {e}")
            return result

    # ─── Original Image Methods ───────────────────────────────────

    def extract_image_url(self, content: str, source_url: str = '') -> Optional[str]:
        """Extract the best image URL from article HTML or steam update content"""
        if not content:
            return None

        # Steam/Valve images
        steam_match = re.search(r'(https://clan\.(?:akamai\.steam|fastly\.steam)static\.com/images/[^\s"<>]+\.(?:jpg|png|gif))', content)
        if steam_match:
            return steam_match.group(1)

        # Generic og:image or img src
        img_match = re.search(r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']', content)
        if img_match:
            url = img_match.group(1)
            if url.startswith('//'):
                url = 'https:' + url
            return url

        # HLTV: construct image from article URL
        if source_url and 'hltv.org' in source_url:
            hltv_match = re.search(r'/(\d+)/', source_url)
            if hltv_match:
                return f"https://img-cdn.hltv.org/gallerypicture/{hltv_match.group(1)}.jpg"

        return None

    def download_image(self, url: str, referer: str = '', retries: int = 2) -> Optional[str]:
        """Download image to temp cache, return local path.
        Converts WebP → JPG automatically (Twitter doesn't accept WebP).
        Retries on failure."""
        try:
            ext = '.jpg'
            for e in ['.png', '.gif', '.webp', '.jpeg']:
                if e in url.lower():
                    ext = e
                    break

            fname = hashlib.md5(url.encode()).hexdigest() + ext
            local_path = MEDIA_CACHE / fname
            # If we already converted this to jpg, check for that too
            jpg_path = MEDIA_CACHE / (hashlib.md5(url.encode()).hexdigest() + '.jpg')
            if jpg_path.exists():
                return str(jpg_path)
            if local_path.exists() and ext != '.webp':
                return str(local_path)

            headers = {'Accept': 'image/*'}
            if referer:
                headers['Referer'] = referer

            last_err = None
            for attempt in range(retries + 1):
                try:
                    r = self._curl.get(url, timeout=15, headers=headers)
                    if r.status_code == 200 and len(r.content) >= 1000:
                        with open(local_path, 'wb') as f:
                            f.write(r.content)
                        try:
                            probe = Image.open(local_path)
                            width, height = probe.size
                            if width < 500 or height < 280:
                                local_path.unlink(missing_ok=True)
                                last_err = f'image too small ({width}x{height})'
                                continue
                        except Exception as img_err:
                            local_path.unlink(missing_ok=True)
                            last_err = f'invalid image: {img_err}'
                            continue
                        # Convert WebP to JPG for Twitter compatibility
                        if ext == '.webp' or local_path.suffix == '.webp':
                            try:
                                img = Image.open(local_path)
                                img = img.convert('RGB')
                                img.save(jpg_path, 'JPEG', quality=92)
                                local_path.unlink(missing_ok=True)
                                logger.info(f"📸 Downloaded + converted WebP→JPG: {jpg_path.name} ({len(r.content)//1024}KB)")
                                return str(jpg_path)
                            except Exception as conv_err:
                                logger.warning(f"⚠️  WebP conversion failed: {conv_err}")
                                # File might actually be PNG/JPG with .webp extension
                                return str(local_path)
                        logger.info(f"📸 Downloaded image: {fname} ({len(r.content)//1024}KB)")
                        return str(local_path)
                    elif r.status_code != 200:
                        last_err = f"HTTP {r.status_code}"
                    else:
                        last_err = f"too small ({len(r.content)}B)"
                except Exception as e:
                    last_err = str(e)
                if attempt < retries:
                    time.sleep(1)

            logger.warning(f"⚠️  Image download failed ({retries+1} tries): {url[:80]}")
            return None
        except Exception as e:
            logger.warning(f"⚠️  Image download error: {e}")
            return None

    def prepare_video_asset(self, video_path: str) -> Optional[str]:
        """Normalize local videos for X native upload."""
        try:
            source = Path(video_path)
            if not source.exists():
                return None

            if source.suffix.lower() == '.mp4' and source.stat().st_size <= 512 * 1024 * 1024:
                return str(source)

            prepared = source.with_suffix('.twitter.mp4')
            if prepared.exists() and prepared.stat().st_mtime >= source.stat().st_mtime:
                return str(prepared)

            result = subprocess.run(
                [
                    'ffmpeg', '-y', '-i', str(source),
                    '-vf', 'scale=min(1280,iw):-2',
                    '-c:v', 'libx264', '-preset', 'medium', '-crf', '24',
                    '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-b:a', '128k',
                    '-movflags', '+faststart',
                    str(prepared),
                ],
                capture_output=True,
                timeout=180,
            )
            if result.returncode == 0 and prepared.exists():
                logger.info(f"🎬 Prepared video for X upload: {prepared.name}")
                return str(prepared)
        except Exception as e:
            logger.warning(f"⚠️  Video preparation failed: {e}")
        return str(Path(video_path)) if Path(video_path).exists() else None

    def upload_media(self, image_path: str, retries: int = 2, account_bucket: str = 'main') -> Optional[str]:
        """Upload image or video via Tweepy v1.1 and return media_id string."""
        api = self._get_api(account_bucket)
        if not api:
            return None
        upload_path = image_path
        suffix = Path(image_path).suffix.lower()
        is_video = suffix in {'.mp4', '.mov', '.m4v', '.webm'}
        if is_video:
            upload_path = self.prepare_video_asset(image_path)
            if not upload_path:
                return None
        last_err = None
        for attempt in range(retries + 1):
            try:
                if is_video:
                    media = api.media_upload(
                        filename=upload_path,
                        chunked=True,
                        media_category='tweet_video',
                    )
                else:
                    media = api.media_upload(filename=upload_path)
                logger.info(f"\U0001f4e4 Uploaded media: {media.media_id}")
                return str(media.media_id)
            except Exception as e:
                last_err = str(e)
                if attempt < retries:
                    time.sleep(2)
        logger.warning(f"\u26a0\ufe0f  Media upload failed after {retries+1} tries: {last_err}")
        return None

    def get_reaction_image(self, mood: str) -> Optional[str]:
        """Get a reaction image path for a given mood"""
        candidates = self.MOOD_REACTIONS.get(mood, [])
        for fname in candidates:
            path = REACTIONS_DIR / fname
            if path.exists():
                return str(path)
        return None

    # ─── Twitch Live Screenshots ──────────────────────────────────

    def _try_twitch_screenshot(self, event: dict, account_bucket: str = 'main') -> Optional[str]:
        """Try to capture a live Twitch screenshot for a match event.
        Runs the async screenshotter in a sync context. Returns media_id or None.
        Stores local screenshot path in event['_screenshot_path'] for vision analysis."""
        try:
            import asyncio
            from processing.twitch_screenshotter import get_twitch_screenshotter

            screenshotter = get_twitch_screenshotter()

            # Run async capture in event loop
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # Already in an async context — use run_coroutine_threadsafe or nest
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    path = pool.submit(
                        lambda: asyncio.run(screenshotter.get_screenshot_for_event(event))
                    ).result(timeout=60)
            else:
                path = asyncio.run(screenshotter.get_screenshot_for_event(event))

            if path:
                # Store path for downstream vision analysis
                event['_screenshot_path'] = path
                media_id = self.upload_media(path, account_bucket=account_bucket)
                if media_id:
                    logger.info(f"📺 Twitch live screenshot attached!")
                    return media_id
        except Exception as e:
            logger.debug(f"⚠️  Twitch screenshot attempt: {e}")
        return None

    def get_media_for_event(self, event: dict, tweet_text: str = '', account_bucket: str = 'main') -> Optional[str]:
        """
        Get the best image for a tweet.  Priority:
        0. Twitch live screenshot (match_result events — real-time game footage)
        1. Google Images search (player/team/event based)
        2. OG image from article page
        3. HLTV player bodyshot (fallback)
        4. Dynamic team player lookup (last resort)

        Returns media_id string or None.
        """
        category = event.get('category', '')
        source_url = event.get('source_url') or ''
        metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
        if not source_url:
            source_url = metadata.get('source_url') or ''
        prefer_generated_media = bool(metadata.get('prefer_generated_media'))
        skip_external_media = prefer_generated_media or ('bo3.gg' in source_url.lower())
        allow_external_media = self._allows_external_media(event, tweet_text)
        is_social_source_url = any(
            domain in source_url.lower()
            for domain in ('twitter.com', 'x.com', 't.co/')
        )

        if not allow_external_media:
            return None

        media_query = self._build_image_search_query(event, tweet_text)

        # 0. Twitch live screenshot — real-time game footage for match results
        if category == 'match_result':
            try:
                media_id = self._try_twitch_screenshot(event, account_bucket=account_bucket)
                if media_id:
                    return media_id
            except Exception as e:
                logger.warning(f"⚠️  Twitch screenshot failed (non-fatal): {e}")

        # 1. OG image from article page — ALWAYS try first when source_url exists.
        # OG images are curated by the article author and always show the correct
        # event/tournament. Google Images often returns photos from wrong years
        # (e.g. PGL Astana 2025 for a PGL Bucharest 2026 article).
        if source_url and not skip_external_media and not is_social_source_url:
            try:
                page_data = self.fetch_article_page(source_url)
                og_url = page_data.get('og_image')
                if og_url:
                    if media_query and self._has_conflicting_season(og_url, media_query):
                        logger.info("⏭️ Skipping OG image with wrong season: %s", og_url[:100])
                        og_url = None
                    elif media_query and not self._url_matches_required_subjects(og_url, media_query):
                        logger.info("⏭️ Skipping OG image without required subject: %s", og_url[:100])
                        og_url = None
                if og_url:
                    local_path = self.download_image(og_url)
                    if local_path:
                        media_id = self.upload_media(local_path, account_bucket=account_bucket)
                        if media_id:
                            event['_external_media_preview_path'] = local_path
                            logger.info(f"📸 OG image from article")
                            return media_id
            except Exception as e:
                logger.warning(f"⚠️  OG image extraction failed (non-fatal): {e}")
        elif source_url and is_social_source_url:
            logger.info("⏭️ Skipping social-card OG image for %s", source_url[:60])

        # 2. Google Images search — good for player-specific photos when OG image unavailable
        if not skip_external_media:
            try:
                media_id = self.get_google_image(event, tweet_text, account_bucket=account_bucket)
                if media_id:
                    return media_id
            except Exception as e:
                logger.warning(f"⚠️  Google Images failed (non-fatal): {e}")

        # 3. HLTV player bodyshot (fallback for match/roster events)
        if category in ('match_result', 'vip_engagement', 'roster_change'):
            try:
                media_id = self.get_best_player_image_for_match(event, tweet_text, account_bucket=account_bucket)
                if media_id:
                    return media_id
            except Exception as e:
                logger.warning(f"\u26a0\ufe0f  HLTV bodyshot failed (non-fatal): {e}")

        # 4. Last resort: scan for known team names \u2192 team player image
        all_text = tweet_text + ' ' + (event.get('headline') or '')
        for team_name in self._find_teams_in_text(all_text)[:1]:
            try:
                media_id = self.get_team_player_image(team_name, account_bucket=account_bucket)
                if media_id:
                    return media_id
            except Exception as e:
                logger.warning(f"\u26a0\ufe0f  Team player lookup failed (non-fatal): {e}")

        return None


_media_manager = None


def get_media_manager() -> MediaManager:
    global _media_manager
    if _media_manager is None:
        _media_manager = MediaManager()
    return _media_manager
