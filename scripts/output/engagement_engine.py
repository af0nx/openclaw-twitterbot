#!/usr/bin/env python3
"""
Engagement Engine — The Growth Machine
Orchestrates proactive engagement strategies beyond just posting tweets.

This is what separates a 50-follower bot from a 100k-follower account.

Strategies:
  1. TREND RIDER — Detect trending CS2 topics, generate reactive content FAST
  2. RATIO HUNTER — Find bad takes from big accounts, craft perfect ratio replies
  3. POLL GENERATOR — Create engagement-bait polls ("Who wins Major?")
  4. THREAD GAME — Weekly "unpopular opinions" or "rate my take" threads
  5. ANNIVERSARY/MILESTONE tracker — "1 year since s1mple retired" type content
  6. CLIP REACTOR — Quote-tweet viral clips with hot takes
  7. CONVERSATION STARTER — Provocative questions that get people replying
  8. FIRST RESPONDER — Be the FIRST reply on big accounts' tweets

The algorithm rewards:
  - Reply-to-like ratio (tweets that get REPLIES are boosted 10x)
  - Dwell time (images/video make people stop scrolling)
  - Quote tweets (spreading your brand to new audiences)
  - Conversation chains (extended back-and-forth = algorithmic gold)
"""

import asyncio
import math
import logging
import os
import json
import random
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
import tweepy

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.meme_generator import get_meme_generator
from processing.openrouter_client import get_openrouter_client
from processing.tweet_quality import (
    normalize_generated_text,
    tweet_quality_issue,
)
from ingestion.style_scraper import get_style_scraper
from utils.db_utils import ensure_db_connection
from utils.twitter_accounts import get_account_credentials

load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ─── Engagement Templates ─────────────────────────────────────────

POLL_TEMPLATES = [
    {
        'question': 'Best AWPer in CS2 right now?',
        'options': ['ZywOo', 'm0NESY', 'sh1ro', 'other'],
    },
    {
        'question': 'Best buy-low team in CS2 right now?',
        'options': ['NaVi', 'FaZe', 'G2', 'Liquid'],
    },
    {
        'question': 'Biggest public trap in CS2 right now?',
        'options': ['FaZe', 'G2', 'Falcons', 'other'],
    },
    {
        'question': 'Which team wins the next Major?',
        'options': ['Spirit', 'Vitality', 'NaVi', 'FaZe'],
    },
    {
        'question': 'Most overrated CS2 team?',
        'options': ['FaZe', 'G2', 'MOUZ', 'Liquid'],
    },
    {
        'question': 'Best map in the current pool?',
        'options': ['Mirage', 'Inferno', 'Nuke', 'Anubis'],
    },
    {
        'question': 'donk in 2 years will be...',
        'options': ['#1 HLTV', 'Top 5 HLTV', 'Overrated', 'Retired'],
    },
    {
        'question': 'CS2 needs _____ most right now',
        'options': ['128 tick', 'Better anti-cheat', 'New operation', 'Source 2 fixes'],
    },
    {
        'question': 'Your rank?',
        'options': ['Silver-GN', 'MG-DMG', 'LE-Global', 'Faceit 7+'],
    },
]

CONVERSATION_STARTERS = [
    "what's the most mispriced team in cs2 right now?",
    "which team does the public keep rating too high?",
    "one player the market still hasn't fully caught up to. who is it?",
    "which org move will change how people price that team the most?",
    "what result from the last month got overreacted to the hardest?",
    "who's the fakest contender in tier 1 right now?",
    "which team is one good event away from a full price reset?",
    "what's the most obvious public trap in cs2 right now?",
    "which lineup looks better than the numbers say?",
    "what's one cs2 take you would actually stake money on?",
    "which team keeps winning without ever looking that clean?",
    "who's the best buy-low team in cs2 today?",
]

MILESTONE_DATES = {
    # Format: (month, day): "event description"
    (1, 1): "Happy New Year! What's your CS2 prediction for this year?",
    (3, 22): "CS:GO launched 12 years ago today. We've come so far.",
    (8, 21): "CS:GO was released on this day in 2012.",
    (9, 27): "CS2 limited test was announced on this day in 2023.",
    (9, 28): "CS2 was fully released on this day in 2023.",
}

# ─── Generator ER Gating ─────────────────────────────────────────

# Maps strategy name → the events.category value used when it posts
STRATEGY_CATEGORY_MAP: Dict[str, str] = {
    'disagreement': 'community_disagreement',
    'poll': 'engagement_poll',
    'conversation': 'engagement_conversation',
    'milestone': 'engagement_milestone',
    'style_take': 'engagement_take',
    'recycle': 'engagement_recycle',
    'match_preview': 'match_preview',
}

# Suppress a generator when its 30-day avg ER is below this floor
# (only applied once we have at least GENERATOR_MIN_SAMPLES data points).
# Set GENERATOR_MIN_ER=0 in .env to disable gating entirely.
GENERATOR_MIN_ER: float = float(os.getenv('GENERATOR_MIN_ER', '0.003'))
GENERATOR_MIN_SAMPLES: int = int(os.getenv('GENERATOR_MIN_SAMPLES', '3'))
GENERATOR_TEMPLATE_TRIAL_BUDGET: int = int(os.getenv('GENERATOR_TEMPLATE_TRIAL_BUDGET', '3'))
GENERATOR_TEMPLATE_MIN_SAMPLES: int = int(os.getenv('GENERATOR_TEMPLATE_MIN_SAMPLES', '3'))
GENERATOR_TEMPLATE_RETIRE_ER: float = float(os.getenv('GENERATOR_TEMPLATE_RETIRE_ER', '0.003'))
GENERATOR_TEMPLATE_EXPLORE_BONUS: float = float(os.getenv('GENERATOR_TEMPLATE_EXPLORE_BONUS', '0.02'))
GENERATOR_TEMPLATE_EPSILON: float = float(os.getenv('GENERATOR_TEMPLATE_EPSILON', '0.15'))

DISAGREEMENT_PATTERNS = [
    (r'\boverrated\b', 'Push back on the overrated label with current form.'),
    (r'\bwashed\b', 'Push back on the washed label with recent results.'),
    (r'\bfraud(s)?\b', 'Challenge the fraud take with recent results.'),
    (r'\bfluke\b', 'Challenge the fluke claim with repeat results.'),
    (r'\bfinished\b', 'Push back on the finished claim with recent form.'),
    (r'\bdead game\b', 'Answer the dead-game take with actual momentum.'),
    (r'\bno chance\b', 'Push back on the no-chance take with recent form.'),
    (r"\b(can('|no)?t|won('|no)?t) win\b", 'Push back with recent winning form.'),
    (r'\bnot top\s*5\b', 'Challenge the ranking claim with current output.'),
    (r'\btier\s*[23]\b', 'Push back on the tier label with recent form.'),
    (r'\blucky\b', 'Challenge the lucky narrative with repeat results.'),
]

TEAM_ALIASES = {
    'NaVi': ['navi', 'natus vincere'],
    'Vitality': ['vitality', 'team vitality'],
    'Spirit': ['spirit', 'team spirit'],
    'FaZe': ['faze', 'faze clan'],
    'G2': ['g2', 'g2 esports'],
    'MOUZ': ['mouz', 'mousesports'],
    'Liquid': ['liquid', 'team liquid'],
    'Astralis': ['astralis'],
    'FURIA': ['furia'],
    'Heroic': ['heroic'],
    'Eternal Fire': ['eternal fire'],
    'Complexity': ['complexity', 'col'],
    'The MongolZ': ['the mongolz', 'mongolz'],
    'Virtus.pro': ['virtus.pro', 'virtus pro', 'vp'],
    'Falcons': ['falcons', 'team falcons'],
}

PLAYER_ALIASES = {
    'ZywOo': ['zywoo'],
    'donk': ['donk'],
    'm0NESY': ['m0nesy', 'monesy'],
    'NiKo': ['niko'],
    's1mple': ['s1mple'],
    'ropz': ['ropz'],
    'sh1ro': ['sh1ro', 'shiro'],
    'torzsi': ['torzsi'],
    'frozen': ['frozen'],
    'b1t': ['b1t', 'bit'],
    'w0nderful': ['w0nderful', 'wonderful'],
}


class EngagementEngine:
    """Proactive engagement strategy engine"""

    def __init__(self):
        self.db_conn = None
        self.client = get_openrouter_client()
        self.meme_gen = get_meme_generator()
        self.style_scraper = get_style_scraper()
        self.twitter_client = None
        self._our_user_id = None
        self._search_api_available = True
        self.disagreement_start_hour = int(os.getenv('DISAGREEMENT_REPLY_START_HOUR', '14'))
        self.disagreement_end_hour = int(os.getenv('DISAGREEMENT_REPLY_END_HOUR', '23'))
        self.disagreement_max_per_day = int(os.getenv('DISAGREEMENT_REPLIES_PER_DAY', '2'))
        self.disagreement_pending_cap = int(os.getenv('DISAGREEMENT_PENDING_CAP', '1'))
        self.disagreement_comment_roots_limit = int(os.getenv('DISAGREEMENT_COMMENT_ROOTS_LIMIT', '4'))
        self.disagreement_comment_search_results = int(os.getenv('DISAGREEMENT_COMMENT_SEARCH_RESULTS', '10'))
        self.disagreement_comment_lookback_hours = int(os.getenv('DISAGREEMENT_COMMENT_LOOKBACK_HOURS', '48'))
        self.disagreement_vip_lookback_hours = int(os.getenv('DISAGREEMENT_VIP_LOOKBACK_HOURS', '36'))
        self.disagreement_vip_limit = int(os.getenv('DISAGREEMENT_VIP_LIMIT', '40'))
        self.disagreement_quote_min_likes = int(os.getenv('DISAGREEMENT_QUOTE_MIN_LIKES', '250'))
        self.disagreement_quote_min_replies = int(os.getenv('DISAGREEMENT_QUOTE_MIN_REPLIES', '40'))
        self.disagreement_quote_chance = float(os.getenv('DISAGREEMENT_QUOTE_CHANCE', '0.2'))
        self._init_twitter_client()

    def connect_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    def _ensure_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    def _init_twitter_client(self):
        try:
            creds = get_account_credentials('replies')
            if not all([
                creds.get('api_key'),
                creds.get('api_secret'),
                creds.get('access_token'),
                creds.get('access_secret'),
            ]):
                self._search_api_available = False
                return

            self.twitter_client = tweepy.Client(
                bearer_token=creds.get('bearer_token'),
                consumer_key=creds['api_key'],
                consumer_secret=creds['api_secret'],
                access_token=creds['access_token'],
                access_token_secret=creds['access_secret'],
                wait_on_rate_limit=True,
            )

            try:
                me = self.twitter_client.get_me()
                if me and me.data:
                    self._our_user_id = me.data.id
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"⚠️  Engagement search client unavailable: {e}")
            self.twitter_client = None
            self._search_api_available = False

    def _extract_entities(self, text: str, aliases: Dict[str, List[str]]) -> List[str]:
        if not text:
            return []

        text_lower = text.lower()
        matches = []
        for canonical, names in aliases.items():
            best_pos = None
            for alias in names:
                match = re.search(rf'(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])', text_lower)
                if match and (best_pos is None or match.start() < best_pos):
                    best_pos = match.start()
            if best_pos is not None:
                matches.append((best_pos, canonical))

        matches.sort(key=lambda item: item[0])
        return [canonical for _, canonical in matches]

    def _looks_disagreement_worthy(self, text: str) -> Optional[str]:
        if not text:
            return None

        text_lower = text.lower()
        for pattern, focus in DISAGREEMENT_PATTERNS:
            if re.search(pattern, text_lower):
                return focus
        return None

    def _safe_int(self, value: Any) -> Optional[int]:
        try:
            return int(str(value).strip())
        except Exception:
            return None

    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            return float(str(value).strip())
        except Exception:
            return None

    def _get_category_avg_er(
        self, category: str, lookback_days: int = 30
    ) -> tuple[Optional[float], int]:
        """Return (avg_engagement_rate, sample_count) for a posted tweet category.

        Returns (None, 0) on DB error or when no samples exist.
        """
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT AVG(t.engagement_rate), COUNT(*)
                    FROM twitter_bot.tweets_v2 t
                    JOIN twitter_bot.events e ON t.event_id = e.id
                    WHERE e.category = %s
                    AND t.status = 'posted'
                    AND t.engagement_rate IS NOT NULL
                    AND t.posted_at > NOW() - (%s || ' days')::interval
                    """,
                    (category, str(lookback_days)),
                )
                row = cur.fetchone()
                if row and row[1]:
                    avg_er = float(row[0]) if row[0] is not None else None
                    return avg_er, int(row[1])
            return None, 0
        except Exception as e:
            logger.warning(f"⚠️  ER lookup for '{category}' failed: {e}")
            return None, 0

    def _slugify_template_seed(self, seed: str, max_parts: int = 8) -> str:
        cleaned = re.sub(r'[^a-z0-9]+', '-', (seed or '').lower()).strip('-')
        parts = [part for part in cleaned.split('-') if part]
        return '-'.join(parts[:max_parts]) or 'template'

    def _build_template_key(self, strategy: str, seed: str) -> str:
        return f"{strategy}:{self._slugify_template_seed(seed)}"

    def _clean_generated_text(self, text: str, label: str, max_chars: int = 280) -> Optional[str]:
        cleaned = normalize_generated_text(text).strip('"').strip("'")
        if max_chars and len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars - 3].rstrip() + "..."
        issue = tweet_quality_issue(cleaned)
        if issue:
            logger.warning(f"⏭️  Rejected {label}: {issue} — {cleaned[:100]}")
            return None
        return cleaned

    def _get_template_stats(
        self, category: str, lookback_days: int = 30
    ) -> Dict[str, Dict[str, Any]]:
        """Return engagement stats keyed by events.metadata.template_key.

        Prefers the rollup materialized view when present, then falls back to a live query.
        """
        self._ensure_db()
        stats: Dict[str, Dict[str, Any]] = {}

        try:
            with self.db_conn.cursor() as cur:
                try:
                    cur.execute(
                        """
                        SELECT template_key, avg_er, tweet_count
                        FROM twitter_bot.mv_generator_template_30d
                        WHERE category = %s
                        """,
                        (category,),
                    )
                    rows = cur.fetchall()
                except Exception:
                    self.db_conn.rollback()
                    cur.execute(
                        """
                        SELECT COALESCE(e.metadata->>'template_key', ''),
                               AVG(t.engagement_rate),
                               COUNT(*)
                        FROM twitter_bot.tweets_v2 t
                        JOIN twitter_bot.events e ON t.event_id = e.id
                        WHERE e.category = %s
                        AND t.status = 'posted'
                        AND t.engagement_rate IS NOT NULL
                        AND t.posted_at > NOW() - (%s || ' days')::interval
                        GROUP BY 1
                        """,
                        (category, str(lookback_days)),
                    )
                    rows = cur.fetchall()

            for template_key, avg_er, tweet_count in rows:
                stats[str(template_key or '')] = {
                    'avg_er': float(avg_er) if avg_er is not None else None,
                    'sample_count': int(tweet_count or 0),
                }
        except Exception as e:
            logger.warning(f"⚠️  Template stats lookup for '{category}' failed: {e}")

        return stats

    def _pick_template_variant(
        self,
        strategy: str,
        category: str,
        candidates: List[Any],
        seed_builder,
    ) -> Optional[tuple[Any, str]]:
        """Pick the next template using a simple bandit-style policy.

        New variants get a short trial budget. Bad variants are retired.
        Proven variants get most of the traffic, with a small exploration rate.
        """
        if not candidates:
            return None

        stats = self._get_template_stats(category)
        exploratory: List[tuple[int, float, Any, str]] = []
        established: List[tuple[float, float, Any, str]] = []

        for candidate in candidates:
            seed = seed_builder(candidate)
            template_key = self._build_template_key(strategy, seed)
            template_stats = stats.get(template_key, {})
            avg_er = template_stats.get('avg_er')
            sample_count = int(template_stats.get('sample_count', 0) or 0)

            if (
                sample_count >= GENERATOR_TEMPLATE_MIN_SAMPLES
                and avg_er is not None
                and avg_er < GENERATOR_TEMPLATE_RETIRE_ER
            ):
                logger.info(
                    f"⏭️  Template '{template_key}' retired — avg ER {avg_er:.4f} "
                    f"< {GENERATOR_TEMPLATE_RETIRE_ER:.4f} (n={sample_count})"
                )
                continue

            if sample_count < GENERATOR_TEMPLATE_TRIAL_BUDGET:
                exploratory.append((sample_count, random.random(), candidate, template_key))
                continue

            score = (avg_er or 0.0) + (GENERATOR_TEMPLATE_EXPLORE_BONUS / max(1.0, math.sqrt(sample_count)))
            established.append((score, random.random(), candidate, template_key))

        if exploratory:
            exploratory.sort(key=lambda item: (item[0], item[1]))
            choice = exploratory[0]
            return choice[2], choice[3]

        if not established:
            return None

        if random.random() < GENERATOR_TEMPLATE_EPSILON:
            _, _, candidate, template_key = random.choice(established)
            return candidate, template_key

        established.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _, _, candidate, template_key = established[0]
        return candidate, template_key

    def _recent_match_metadata(self, days: int = 45, limit: int = 150) -> List[Dict[str, Any]]:
        self._ensure_db()
        with self.db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT metadata
                FROM twitter_bot.events
                WHERE category = 'match_result'
                AND created_at > NOW() - (%s || ' days')::interval
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (str(days), limit),
            )
            return [row[0] or {} for row in cur.fetchall()]

    def _build_team_evidence_lines(self, teams: List[str]) -> List[str]:
        if not teams:
            return []

        tracked = {team: [] for team in teams}
        for metadata in self._recent_match_metadata():
            if metadata.get('match_status') == 'upcoming':
                continue

            team1 = metadata.get('team1', '')
            team2 = metadata.get('team2', '')
            score1 = self._safe_int(metadata.get('score1'))
            score2 = self._safe_int(metadata.get('score2'))
            if not team1 or not team2 or score1 is None or score2 is None:
                continue

            participants = {}
            for participant in (team1, team2):
                canonical = self._extract_entities(participant, TEAM_ALIASES)
                if canonical:
                    participants[participant] = canonical[0]
            if len(participants) != 2:
                continue

            team1_name = participants[team1]
            team2_name = participants[team2]
            matchup = {
                team1_name: {
                    'won': score1 > score2,
                    'own_score': score1,
                    'opp_score': score2,
                    'opponent': team2_name,
                    'event': metadata.get('event', ''),
                },
                team2_name: {
                    'won': score2 > score1,
                    'own_score': score2,
                    'opp_score': score1,
                    'opponent': team1_name,
                    'event': metadata.get('event', ''),
                },
            }

            for team in teams:
                if team in matchup and len(tracked[team]) < 5:
                    tracked[team].append(matchup[team])

            if all(len(tracked[team]) >= 5 for team in teams):
                break

        evidence = []
        for team in teams:
            rows = tracked.get(team) or []
            if not rows:
                continue
            wins = sum(1 for row in rows if row['won'])
            losses = len(rows) - wins
            evidence.append(f"{team} are {wins}-{losses} in their last {len(rows)} tracked series.")
            latest = rows[0]
            verb = 'beat' if latest['won'] else 'lost to'
            suffix = f" at {latest['event']}" if latest['event'] else ''
            evidence.append(
                f"Their latest tracked result was {latest['own_score']}-{latest['opp_score']} vs {latest['opponent']}{suffix}."
                if latest['won']
                else f"Their latest tracked result was {latest['own_score']}-{latest['opp_score']} against {latest['opponent']}{suffix}."
            )
        return evidence[:4]

    def _build_player_evidence_lines(self, players: List[str]) -> List[str]:
        if not players:
            return []

        tracked = {player: [] for player in players}
        for metadata in self._recent_match_metadata(limit=120):
            match_context = metadata.get('match_context') or {}
            player_stats = match_context.get('player_stats') or metadata.get('player_stats') or []
            if not isinstance(player_stats, list):
                continue

            for stat_line in player_stats:
                name = str((stat_line or {}).get('name', '')).strip()
                rating = self._safe_float((stat_line or {}).get('rating'))
                if not name or rating is None:
                    continue

                player_match = self._extract_entities(name, PLAYER_ALIASES)
                if not player_match:
                    continue

                player = player_match[0]
                if player not in tracked or len(tracked[player]) >= 4:
                    continue

                tracked[player].append({
                    'rating': rating,
                    'event': metadata.get('event', ''),
                })

            if all(len(tracked[player]) >= 3 for player in players):
                break

        evidence = []
        for player in players:
            rows = tracked.get(player) or []
            if not rows:
                continue
            avg_rating = sum(row['rating'] for row in rows) / len(rows)
            evidence.append(f"{player} is averaging a {avg_rating:.2f} rating across their last {len(rows)} tracked matches.")
            latest = rows[0]
            suffix = f" at {latest['event']}" if latest['event'] else ''
            evidence.append(f"{player} posted a {latest['rating']:.2f} rating in the latest tracked series{suffix}.")
        return evidence[:4]

    def _already_targeted(self, tweet_id: str) -> bool:
        self._ensure_db()
        with self.db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM twitter_bot.events
                WHERE category = 'community_disagreement'
                AND metadata->>'tweet_id' = %s
                LIMIT 1
                """,
                (str(tweet_id),),
            )
            if cur.fetchone():
                return True

            cur.execute(
                """
                SELECT 1
                FROM twitter_bot.tweets_v2
                WHERE (reply_target_id = %s OR quote_tweet_id = %s)
                AND created_at > NOW() - INTERVAL '30 days'
                LIMIT 1
                """,
                (str(tweet_id), str(tweet_id)),
            )
            return cur.fetchone() is not None

    def _score_disagreement_candidate(self, candidate: Dict[str, Any]) -> float:
        metrics = candidate.get('engagement_metrics') or {}
        likes = float(metrics.get('likes', 0) or 0)
        replies = float(metrics.get('replies', 0) or 0)
        retweets = float(metrics.get('retweets', 0) or 0)
        type_bonus = 80.0 if candidate.get('target_type') == 'comment' else 0.0
        return type_bonus + (replies * 3.0) + (likes * 0.2) + (retweets * 0.6)

    def _dedupe_candidates(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        deduped = []
        for candidate in candidates:
            tweet_id = str(candidate.get('tweet_id') or '').strip()
            if not tweet_id or tweet_id in seen:
                continue
            seen.add(tweet_id)
            deduped.append(candidate)
        return deduped

    def _get_vip_disagreement_candidates(self) -> List[Dict[str, Any]]:
        self._ensure_db()
        with self.db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT content, metadata
                FROM twitter_bot.events
                WHERE category = 'vip_engagement'
                AND created_at > NOW() - (%s || ' hours')::interval
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (str(self.disagreement_vip_lookback_hours), self.disagreement_vip_limit),
            )
            rows = cur.fetchall()

        candidates = []
        for content, metadata in rows:
            metadata = metadata or {}
            tweet_id = metadata.get('tweet_id')
            if not tweet_id or not content:
                continue
            metrics = metadata.get('engagement_metrics') or {}
            candidates.append({
                'tweet_id': str(tweet_id),
                'author': metadata.get('vip_username') or 'unknown',
                'text': content,
                'target_type': 'post',
                'engagement_metrics': {
                    'likes': self._safe_int(metrics.get('likes')) or 0,
                    'replies': self._safe_int(metrics.get('replies')) or 0,
                    'retweets': self._safe_int(metrics.get('retweets')) or 0,
                },
            })

        candidates.sort(key=self._score_disagreement_candidate, reverse=True)
        return self._dedupe_candidates(candidates)

    def _get_recent_comment_candidates(self) -> List[Dict[str, Any]]:
        if not self.twitter_client or not self._search_api_available:
            return []

        self._ensure_db()
        with self.db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT twitter_tweet_id
                FROM twitter_bot.tweets_v2
                WHERE status = 'posted'
                AND twitter_tweet_id IS NOT NULL
                AND posted_at > NOW() - (%s || ' hours')::interval
                ORDER BY posted_at DESC
                LIMIT %s
                """,
                (str(self.disagreement_comment_lookback_hours), self.disagreement_comment_roots_limit),
            )
            root_ids = [row[0] for row in cur.fetchall()]

        candidates = []
        for root_id in root_ids:
            try:
                results = self.twitter_client.search_recent_tweets(
                    query=f"conversation_id:{root_id} -is:retweet",
                    max_results=self.disagreement_comment_search_results,
                    tweet_fields=['author_id', 'created_at', 'public_metrics'],
                    expansions=['author_id'],
                    user_fields=['username'],
                )
                if not results or not results.data:
                    continue

                users = {}
                if results.includes and 'users' in results.includes:
                    users = {user.id: user.username for user in results.includes['users']}

                for tweet in results.data:
                    if str(tweet.id) == str(root_id):
                        continue
                    if self._our_user_id and tweet.author_id == self._our_user_id:
                        continue

                    metrics = getattr(tweet, 'public_metrics', {}) or {}
                    candidates.append({
                        'tweet_id': str(tweet.id),
                        'author': users.get(tweet.author_id, 'unknown'),
                        'text': tweet.text,
                        'target_type': 'comment',
                        'engagement_metrics': {
                            'likes': self._safe_int(metrics.get('like_count')) or 0,
                            'replies': self._safe_int(metrics.get('reply_count')) or 0,
                            'retweets': self._safe_int(metrics.get('retweet_count')) or 0,
                        },
                    })
            except tweepy.TweepyException as e:
                err = str(e)
                if '401' in err or '403' in err:
                    self._search_api_available = False
                    logger.debug("Comment search unavailable on this X tier")
                    break
                if '429' in err:
                    logger.warning("⚠️  Comment search rate limited")
                    break
                logger.warning(f"⚠️  Comment search failed: {e}")
            except Exception as e:
                logger.warning(f"⚠️  Comment search failed: {e}")

        candidates.sort(key=self._score_disagreement_candidate, reverse=True)
        return self._dedupe_candidates(candidates)

    async def maybe_post_disagreement_reply(self) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        if now.hour < self.disagreement_start_hour or now.hour > self.disagreement_end_hour:
            return None

        self._ensure_db()
        with self.db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM twitter_bot.tweets_v2
                WHERE pillar = 16
                AND created_at > NOW() - INTERVAL '24 hours'
                """
            )
            if cur.fetchone()[0] >= self.disagreement_max_per_day:
                return None

            cur.execute(
                """
                SELECT COUNT(*)
                FROM twitter_bot.events
                WHERE category = 'community_disagreement'
                AND status = 'pending'
                AND created_at > NOW() - INTERVAL '12 hours'
                """
            )
            if cur.fetchone()[0] >= self.disagreement_pending_cap:
                return None

        candidates = self._get_recent_comment_candidates()
        candidates.extend(self._get_vip_disagreement_candidates())
        candidates = self._dedupe_candidates(candidates)
        candidates.sort(key=self._score_disagreement_candidate, reverse=True)

        for candidate in candidates:
            tweet_id = candidate['tweet_id']
            if self._already_targeted(tweet_id):
                continue

            focus = self._looks_disagreement_worthy(candidate['text'])
            if not focus:
                continue

            teams = self._extract_entities(candidate['text'], TEAM_ALIASES)
            players = self._extract_entities(candidate['text'], PLAYER_ALIASES)
            evidence_lines = self._build_team_evidence_lines(teams)

            for line in self._build_player_evidence_lines(players):
                if line not in evidence_lines:
                    evidence_lines.append(line)

            if not evidence_lines:
                continue

            metrics = candidate.get('engagement_metrics') or {}
            reply_mode = 'reply'
            if (
                candidate['target_type'] == 'post'
                and (
                    metrics.get('likes', 0) >= self.disagreement_quote_min_likes
                    or metrics.get('replies', 0) >= self.disagreement_quote_min_replies
                )
                and random.random() < self.disagreement_quote_chance
            ):
                reply_mode = 'quote'

            text = re.sub(r'\s+', ' ', candidate['text']).strip()[:280]
            return {
                'headline': f"Disagreement reply to @{candidate['author']}",
                'content': text,
                'category': 'community_disagreement',
                'urgency': 'important' if candidate['target_type'] == 'comment' else 'normal',
                'source': 'engagement_engine',
                'metadata': {
                    'strategy': 'community_disagreement',
                    'tweet_id': tweet_id,
                    'target_username': candidate['author'],
                    'target_type': candidate['target_type'],
                    'focus': focus,
                    'reply_mode': reply_mode,
                    'evidence_lines': evidence_lines[:3],
                    'target_teams': teams,
                    'target_players': players,
                },
            }

        return None

    # ─── Strategy 1: Poll Generator ───────────────────────────────

    async def maybe_post_poll(self) -> Optional[Dict]:
        """
        Generate a poll tweet. Polls get massive engagement because
        people LOVE voting and then arguing in replies.

        X API v2 supports polls natively.
        Returns event dict to insert into queue, or None.
        """
        now = datetime.now(timezone.utc)

        # Only post polls during peak hours (15:00-23:00 UTC)
        if now.hour < 15 or now.hour > 22:
            return None

        # Max 1 poll per day (check both tweets and pending events)
        self._ensure_db()
        with self.db_conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM twitter_bot.tweets_v2
                WHERE pillar = 13 AND created_at > NOW() - INTERVAL '24 hours'
            """)
            if cur.fetchone()[0] > 0:
                return None
            cur.execute("""
                SELECT COUNT(*) FROM twitter_bot.events
                WHERE category = 'engagement_poll' AND status = 'pending'
                AND created_at > NOW() - INTERVAL '24 hours'
            """)
            if cur.fetchone()[0] > 0:
                return None

        selected = self._pick_template_variant(
            strategy='poll',
            category='engagement_poll',
            candidates=POLL_TEMPLATES,
            seed_builder=lambda template: f"{template['question']} {' '.join(template['options'])}",
        )
        if not selected:
            return None

        template, template_key = selected

        try:
            result = self.client.generate(
                prompt=(
                    f"Original poll question: \"{template['question']}\"\n"
                    f"Options: {', '.join(template['options'])}\n\n"
                    "Make this poll question more engaging and current. "
                    "Keep it short (under 100 chars). Sound like a sharp CS2 trader, not a brand.\n"
                    "Good angles: buy low, sell high, public trap, overreaction, market reset.\n"
                    "Return ONLY the question text. Nothing else."
                ),
                tier='eco',
                temperature=0.9,
                max_tokens=60
            )
            question = self._clean_generated_text(result['text'], 'poll rewrite', max_chars=100)
        except Exception:
            question = None

        if not question:
            question = self._clean_generated_text(template['question'], 'poll template', max_chars=100)
        if not question:
            return None

        return {
            'headline': question,
            'content': question,
            'category': 'engagement_poll',
            'urgency': 'normal',
            'source': 'engagement_engine',
            'metadata': {
                'poll_options': template['options'],
                'poll_duration_minutes': 1440,  # 24 hours
                'strategy': 'poll_generator',
                'template_key': template_key,
            }
        }

    # ─── Strategy 2: Conversation Starters ─────────────────────────

    async def maybe_post_conversation_starter(self) -> Optional[Dict]:
        """
        Post a provocative question to drive replies.
        Replies are the #1 signal X algorithm uses for distribution.
        """
        now = datetime.now(timezone.utc)

        # Best time: 17:00-21:00 UTC (EU/NA overlap)
        if now.hour < 17 or now.hour > 20:
            return None

        # Max 1 per day (check both tweets and pending events)
        self._ensure_db()
        with self.db_conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM twitter_bot.tweets_v2
                WHERE pillar = 14 AND created_at > NOW() - INTERVAL '24 hours'
            """)
            if cur.fetchone()[0] > 0:
                return None
            cur.execute("""
                SELECT COUNT(*) FROM twitter_bot.events
                WHERE category = 'engagement_conversation' AND status = 'pending'
                AND created_at > NOW() - INTERVAL '24 hours'
            """)
            if cur.fetchone()[0] > 0:
                return None

        selected = self._pick_template_variant(
            strategy='conversation',
            category='engagement_conversation',
            candidates=CONVERSATION_STARTERS,
            seed_builder=lambda starter: starter,
        )
        if not selected:
            return None

        starter, template_key = selected

        try:
            result = self.client.generate(
                prompt=(
                    f"Original tweet: \"{starter}\"\n\n"
                    "Rewrite to sound natural and casual. Sound like a sharp CS2 trader starting an argument.\n"
                    "Good angles: line moves, market panic, buy low, sell high, public trap, overreaction.\n"
                    "Max 250 chars. No hashtags. No emojis spam (1 max).\n"
                    "Do NOT add generic questions like 'what do you think?' at the end.\n"
                    "Return ONLY the tweet text."
                ),
                tier='eco',
                temperature=0.9,
                max_tokens=80
            )
            text = self._clean_generated_text(result['text'], 'conversation rewrite', max_chars=250)
        except Exception:
            text = None

        if not text:
            text = self._clean_generated_text(starter, 'conversation template', max_chars=250)
        if not text:
            return None

        return {
            'headline': text,
            'content': text,
            'category': 'engagement_conversation',
            'urgency': 'normal',
            'source': 'engagement_engine',
            'metadata': {
                'strategy': 'conversation_starter',
                'template_key': template_key,
            }
        }

    # ─── Strategy 3: Milestone/Anniversary Posts ──────────────────

    async def maybe_post_milestone(self) -> Optional[Dict]:
        """Post anniversary/milestone content. These get massive nostalgia engagement."""
        now = datetime.now(timezone.utc)
        key = (now.month, now.day)

        template = MILESTONE_DATES.get(key)
        if not template:
            return None

        # Only once per milestone per day
        self._ensure_db()
        with self.db_conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM twitter_bot.tweets_v2
                WHERE content LIKE %s AND created_at > NOW() - INTERVAL '24 hours'
            """, (f"%{template[:30]}%",))
            if cur.fetchone()[0] > 0:
                return None

        return {
            'headline': template,
            'content': template,
            'category': 'engagement_milestone',
            'urgency': 'normal',
            'source': 'engagement_engine',
            'metadata': {
                'strategy': 'milestone',
                'date': now.strftime('%Y-%m-%d'),
                'template_key': self._build_template_key('milestone', template),
            }
        }

    # ─── Strategy 4: Match Preview Cards ──────────────────────────

    async def generate_match_preview_events(self) -> List[Dict]:
        """
        Generate pre-match preview content with VS cards.
        Pre-match content gets engagement because fans are excited and opinionated.
        """
        events = []

        # Check for upcoming T1 matches in events table (from HLTV monitor)
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT id, headline, content, metadata
                    FROM twitter_bot.events
                    WHERE category = 'match_result'
                    AND status = 'pending'
                    AND metadata->>'match_status' = 'upcoming'
                    AND created_at > NOW() - INTERVAL '6 hours'
                    LIMIT 3
                """)

                for row in cur.fetchall():
                    metadata = row[3] or {}
                    team1 = metadata.get('team1', '')
                    team2 = metadata.get('team2', '')
                    event_name = metadata.get('event', '')

                    if team1 and team2:
                        # Generate VS card
                        img_path = self.meme_gen.generate_vs_card(
                            team1, team2, event_name=event_name
                        )
                        events.append({
                            'headline': f"{team1} vs {team2}",
                            'content': f"Preview: {team1} vs {team2} at {event_name}",
                            'category': 'match_preview',
                            'urgency': 'normal',
                            'source': 'engagement_engine',
                            'metadata': {
                                'strategy': 'match_preview',
                                'team1': team1,
                                'team2': team2,
                                'event': event_name,
                                'media_path': img_path,
                                'template_key': self._build_template_key('match_preview', f'{team1}-{team2}'),
                            }
                        })
        except Exception as e:
            logger.warning(f"⚠️  Match preview query failed: {e}")

        return events

    # ─── Strategy 5: Scoreboard Cards for Results ─────────────────

    def generate_scoreboard_for_event(self, event: Dict) -> Optional[str]:
        """
        Called by tweet_scheduler after a match result.
        Generates a scoreboard card image and returns media path.
        """
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            return None

        team1 = metadata.get('team1', '')
        team2 = metadata.get('team2', '')
        if not team1 or not team2:
            return None

        maps_data = metadata.get('maps', [])
        event_name = metadata.get('event', '')

        if maps_data and len(maps_data) > 0:
            return self.meme_gen.generate_scoreboard_card(
                team1, team2, maps_data, event_name
            )
        else:
            score1 = metadata.get('score1', '')
            score2 = metadata.get('score2', '')
            if score1 and score2:
                return self.meme_gen.generate_vs_card(
                    team1, team2, str(score1), str(score2), event_name
                )
        return None

    # ─── Strategy 6: Style-Informed Hot Takes ─────────────────────

    async def generate_style_informed_take(self) -> Optional[Dict]:
        """
        Use the style bank to generate a tweet that MIMICS the format
        of tweets that actually went viral.
        """
        examples = self.style_scraper.get_style_examples(count=5)
        if not examples:
            return None

        now = datetime.now(timezone.utc)
        if now.hour < 15 or now.hour > 22:
            return None

        # Max 2 style-informed takes per day (check both tweets and pending events)
        self._ensure_db()
        with self.db_conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM twitter_bot.tweets_v2
                WHERE pillar = 15 AND created_at > NOW() - INTERVAL '24 hours'
            """)
            if cur.fetchone()[0] >= 2:
                return None
            cur.execute("""
                SELECT COUNT(*) FROM twitter_bot.events
                WHERE category = 'engagement_take' AND status = 'pending'
                AND created_at > NOW() - INTERVAL '24 hours'
            """)
            if cur.fetchone()[0] >= 2:
                return None

        try:
            examples_text = "\n".join(examples[:5])

            # Inject recent event headlines so the LLM knows what's ACTUALLY happening
            # Without this, the model hallucinates stale player/team associations (e.g. NiKo on G2)
            recent_headlines = []
            try:
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        SELECT headline FROM twitter_bot.events
                        WHERE status IN ('processed', 'scheduled', 'posted')
                        AND created_at > NOW() - INTERVAL '24 hours'
                        AND source != 'engagement_engine'
                        AND category NOT IN ('match_prediction')
                        ORDER BY created_at DESC LIMIT 10
                    """)
                    recent_headlines = [r[0] for r in cur.fetchall() if r[0]]
            except Exception as e:
                logger.warning(f"⚠️  Failed to fetch headlines for style take: {e}")

            # If we have NO real headlines, refuse to generate — the LLM WILL fabricate
            if not recent_headlines:
                logger.warning("⏭️  No recent real headlines — skipping style take to prevent fabrication")
                return None
            
            context_block = (
                "RECENT REAL CS2 NEWS (reference ONLY these — do NOT invent events):\n"
                + "\n".join(f"- {h}" for h in recent_headlines)
                + "\n\n"
            )

            result = self.client.generate(
                prompt=(
                    "Here are viral CS2 tweets that got thousands of likes:\n\n"
                    f"{examples_text}\n\n"
                    f"{context_block}"
                    "Study the FORMAT. Study the LENGTH. Study the ENERGY.\n"
                    "Now write ONE original CS2 tweet in the same style.\n"
                    "CRITICAL RULES:\n"
                    "- NEVER invent match results, scores, or outcomes. NEVER say a team beat/lost/swept another unless it's in the news above.\n"
                    "- You CAN write opinions, hype, hot takes, or predictions about the news above.\n"
                    "- Do NOT assume which team a player is on unless stated in the news above.\n"
                    "- Best angles: market overreaction, buy low, sell high, public trap, price reset, line move only if grounded in the news.\n"
                    "- Sound like a sharp CS2 trader reading the room early, not a brand.\n"
                    "Max 140 chars.\n"
                    "Return ONLY the tweet text."
                ),
                tier='auto',
                temperature=0.9,
                max_tokens=60
            )
            text = self._clean_generated_text(result['text'], 'style take', max_chars=140)
        except Exception as e:
            logger.warning(f"⚠️  Style take generation failed: {e}")
            return None

        if not text:
            return None

        return {
            'headline': text,
            'content': text,
            'category': 'engagement_take',
            'urgency': 'normal',
            'source': 'engagement_engine',
            'metadata': {
                'strategy': 'style_informed_take',
                'style_examples_used': len(examples),
                'template_key': self._build_template_key('style_take', examples[0] if examples else 'style-bank'),
            }
        }

    # ─── Strategy 7: Best Performing Tweet Repeat ─────────────────

    async def maybe_recycle_banger(self) -> Optional[Dict]:
        """
        Find your best-performing tweet formats and create NEW tweets
        in the same style. Not reposting — creating new content
        that follows the same pattern.
        
        IMPORTANT: Only generates opinions/hype — NEVER claims specific
        match results, scores, or events that may not have happened.
        """
        self._ensure_db()
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT t.content, t.engagement_rate, t.likes, t.pillar, e.category
                    FROM twitter_bot.tweets_v2 t
                    JOIN twitter_bot.events e ON t.event_id = e.id
                    WHERE t.status = 'posted'
                    AND t.engagement_rate > 0.03
                    AND t.posted_at > NOW() - INTERVAL '30 days'
                    ORDER BY t.engagement_rate DESC
                    LIMIT 5
                """)
                bangers = cur.fetchall()

            if len(bangers) < 2:
                return None

            banger_text = "\n".join([f'- "{r[0]}" (ER: {r[1]:.3f}, {r[2]} likes)' for r in bangers])

            # Inject recent real headlines so the LLM knows what's actually happening
            recent_headlines = []
            try:
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        SELECT headline FROM twitter_bot.events
                        WHERE status IN ('processed', 'scheduled', 'posted')
                        AND created_at > NOW() - INTERVAL '24 hours'
                        AND source != 'engagement_engine'
                        AND category NOT IN ('match_prediction')
                        ORDER BY created_at DESC LIMIT 10
                    """)
                    recent_headlines = [r[0] for r in cur.fetchall() if r[0]]
            except Exception as e:
                logger.warning(f"⚠️  Failed to fetch headlines for recycle: {e}")

            # If we have NO real headlines, refuse to generate — the LLM WILL fabricate
            if not recent_headlines:
                logger.warning("⏭️  No recent real headlines — skipping recycle to prevent fabrication")
                return None

            context_block = (
                "\nRECENT REAL CS2 NEWS (reference ONLY these if mentioning specific events):\n"
                + "\n".join(f"- {h}" for h in recent_headlines)
                + "\n\n"
            )

            result = self.client.generate(
                prompt=(
                    "These are OUR best-performing tweets:\n\n"
                    f"{banger_text}\n\n"
                    "Study what made them work — the FORMAT, the energy, the length.\n"
                    f"{context_block}"
                    "Now create ONE new tweet in the same STYLE.\n"
                    "CRITICAL RULES:\n"
                    "- NEVER invent match results, scores, reverse sweeps, or clutch plays that you are not CERTAIN happened\n"
                    "- NEVER claim a team beat/lost to another team unless it appears in the recent news above\n"
                    "- You CAN write opinions, hype, hot takes, or general CS2 commentary\n"
                    "- You CAN reference real events from the recent news above\n"
                    "- Best angles: market overreaction, buy low, sell high, public trap, price reset\n"
                    "- Sound like a sharp CS2 trader, not a brand.\n"
                    "- Max 140 chars.\n"
                    "Return ONLY the tweet text."
                ),
                tier='auto',
                temperature=0.85,
                max_tokens=60
            )
            text = self._clean_generated_text(result['text'], 'recycle take', max_chars=140)
            if not text:
                return None

            return {
                'headline': text,
                'content': text,
                'category': 'engagement_recycle',
                'urgency': 'normal',
                'source': 'engagement_engine',
                'metadata': {
                    'strategy': 'recycle_banger',
                    'inspired_by_count': len(bangers),
                    'template_key': self._build_template_key('recycle', bangers[0][0] if bangers else 'recycle-bank'),
                }
            }
        except Exception as e:
            logger.warning(f"⚠️  Banger recycle failed: {e}")
            return None

    # ─── Orchestrator ─────────────────────────────────────────────

    async def run_cycle(self):
        """Run all engagement strategies. Creates events in the pipeline."""
        self.connect_db()
        # Rollback any stale implicit transaction so CURRENT_DATE/NOW() are fresh
        try:
            self.db_conn.rollback()
        except Exception:
            pass
        self.style_scraper.connect_db()

        generated = []

        # Run strategies
        # Polls average 29 impressions (3x news). Re-enabled April 2026.
        strategies = [
            ('disagreement', self.maybe_post_disagreement_reply),
            ('poll', self.maybe_post_poll),
            ('conversation', self.maybe_post_conversation_starter),
            ('milestone', self.maybe_post_milestone),
            ('style_take', self.generate_style_informed_take),
            ('recycle', self.maybe_recycle_banger),
        ]

        for name, strategy_fn in strategies:
            try:
                # ── ER gate: suppress zero-engagement generators ──────────────
                if GENERATOR_MIN_ER > 0:
                    category = STRATEGY_CATEGORY_MAP.get(name)
                    if category:
                        avg_er, sample_count = self._get_category_avg_er(category)
                        if (
                            sample_count >= GENERATOR_MIN_SAMPLES
                            and avg_er is not None
                            and avg_er < GENERATOR_MIN_ER
                        ):
                            logger.info(
                                f"⏭️  Generator '{name}' suppressed — "
                                f"30d avg ER {avg_er:.4f} < threshold {GENERATOR_MIN_ER:.4f} "
                                f"(n={sample_count})"
                            )
                            continue

                result = await strategy_fn()
                if result:
                    generated.append(result)
                    logger.info(f"✨ Engagement strategy '{name}' generated content")
            except Exception as e:
                logger.warning(f"⚠️  Strategy '{name}' failed: {e}")

        # Match previews (can generate multiple)
        try:
            previews = await self.generate_match_preview_events()
            generated.extend(previews)
        except Exception as e:
            logger.warning(f"⚠️  Match previews failed: {e}")

        # Insert generated events into the pipeline
        for event_data in generated:
            try:
                headline = normalize_generated_text(event_data.get('headline', ''))
                content = normalize_generated_text(event_data.get('content', ''))
                payload_issue = tweet_quality_issue(content) or tweet_quality_issue(headline)
                if payload_issue:
                    logger.warning(
                        "⏭️  Dropping generated event before insert: %s — %s",
                        payload_issue,
                        content[:100] or headline[:100],
                    )
                    continue

                self._ensure_db()
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO twitter_bot.events
                        (headline, content, source, category, urgency, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (
                        headline,
                        content,
                        event_data.get('source', 'engagement_engine'),
                        event_data['category'],
                        event_data.get('urgency', 'normal'),
                        Json(event_data.get('metadata', {})),
                    ))
                    event_id = cur.fetchone()[0]
                    self.db_conn.commit()
                    logger.info(f"📥 Engagement event created: {event_id} ({event_data['category']})")
            except Exception as e:
                logger.error(f"❌ Failed to insert engagement event: {e}")
                try:
                    self.db_conn.rollback()
                except Exception:
                    pass

        logger.info(f"🎯 Engagement cycle complete: {len(generated)} events generated")

    async def run_forever(self):
        """Main loop — every 1 hour (peak and off-peak)"""
        logger.info("🚀 Engagement Engine started — growing the account")
        consecutive_errors = 0
        while True:
            try:
                await self.run_cycle()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"❌ Cycle failed: {e}")
                # Back off on repeated errors to avoid crash-loop spam
                if consecutive_errors >= 3:
                    logger.warning(f"⏸️  {consecutive_errors} consecutive errors — sleeping 30 min")
                    await asyncio.sleep(1800)
                    consecutive_errors = 0
                    continue

            now = datetime.now(timezone.utc)
            peak_interval = int(os.getenv('ENGAGEMENT_ENGINE_PEAK_INTERVAL_HOURS', '1'))
            offpeak_interval = int(os.getenv('ENGAGEMENT_ENGINE_OFFPEAK_INTERVAL_HOURS', '1'))
            if 15 <= now.hour <= 22:
                await asyncio.sleep(peak_interval * 3600)
            else:
                await asyncio.sleep(offpeak_interval * 3600)


if __name__ == '__main__':
    asyncio.run(EngagementEngine().run_forever())
