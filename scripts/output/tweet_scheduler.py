#!/usr/bin/env python3
"""
Tweet Scheduler - Twitter Bot Pipeline V2
Manages the tweet queue and enforces strict 100/day API cap (Pay Per Use tier)
Pre-commit reservation system to prevent quota violations
"""

import asyncio
import logging
import random
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
import os

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
import json

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.content_generator import ContentGenerator
from processing.fact_checker import get_fact_checker
from processing.tone_validator import get_tone_validator
from processing.mirofish_guard import get_mirofish_guard
from processing.media_manager import get_media_manager
from processing.meme_generator import get_meme_generator
from processing.hashtag_injector import inject_hashtags, inject_hashtags_thread
from processing.match_analyzer import get_match_analyzer
from utils.account_quota import free_account_slot, reserve_account_slot
from utils.runtime_schema import ensure_runtime_schema_extensions
from utils.twitter_accounts import get_bucket_daily_cap, select_account_bucket
from utils.db_utils import ensure_db_connection
from utils.cs2_constants import CS2_KEYWORDS, NON_CS2_KEYWORDS, is_t1_content, is_cs2_relevant

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TweetScheduler:
    """Central scheduler with pre-commit reservation system"""
    
    def __init__(self):
        self.db_conn = None
        self.generator = ContentGenerator()
        self.fact_checker = get_fact_checker()
        self.tone_validator = get_tone_validator()
        self.mirofish_guard = get_mirofish_guard()
        self.media_manager = get_media_manager()
        self.meme_generator = get_meme_generator()
        self.match_analyzer = get_match_analyzer()
        self._schema_ready = False
        
        self.daily_cap = int(os.getenv('DAILY_TWEET_CAP', 95))
        self.dry_run = os.getenv('DRY_RUN_MODE', 'false').lower() == 'true'
        
        # Peak hours (UTC): EU evening 15:00-20:00, NA afternoon 17:00-23:00
        self.peak_hours = list(range(15, 24))  # 15:00-23:00 UTC covers both regions
        
    def connect_db(self):
        """Establish PostgreSQL connection with auto-reconnect"""
        try:
            self.db_conn = ensure_db_connection(self.db_conn)
            if not self._schema_ready:
                ensure_runtime_schema_extensions(self.db_conn)
                self._schema_ready = True
            self.generator.connect_db()
            self.match_analyzer.connect_db(self.db_conn)
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise

    def _ensure_db(self):
        """Lightweight reconnect guard — call before every DB operation"""
        self.db_conn = ensure_db_connection(self.db_conn)
        if not self._schema_ready:
            ensure_runtime_schema_extensions(self.db_conn)
            self._schema_ready = True
    
    def reserve_slot(self, bucket: str) -> bool:
        """
        Reserve a slot in today's quota for a specific account bucket.
        
        Returns:
            True if slot reserved, False if quota exhausted
        """
        try:
            daily_cap = get_bucket_daily_cap(bucket)
            if reserve_account_slot(self.db_conn, bucket, daily_cap):
                logger.info(f"🎟️  Reserved slot for {bucket} bucket (cap {daily_cap}/day)")
                return True
            logger.warning(f"🚫 Quota exhausted for {bucket} bucket")
            return False
                
        except Exception as e:
            logger.error(f"❌ Failed to reserve slot for {bucket}: {e}")
            self.db_conn.rollback()
            return False
    
    def free_slot(self, bucket: str):
        """Free a reserved slot (on rejection/veto)"""
        try:
            free_account_slot(self.db_conn, bucket)
            logger.info(f"🎟️  Freed reserved slot for {bucket} bucket")
        except Exception as e:
            logger.error(f"❌ Failed to free slot for {bucket}: {e}")

    def _minutes_since_last_post(self) -> Optional[float]:
        """Return minutes since the most recent posted tweet, or None if no posts."""
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT EXTRACT(EPOCH FROM (NOW() - MAX(posted_at)))/60
                    FROM twitter_bot.tweets_v2
                    WHERE posted_at IS NOT NULL
                """)
                row = cur.fetchone()
                return row[0] if row and row[0] is not None else None
        except Exception as e:
            logger.warning(f"⚠️  Could not check last post time: {e}")
            return None

    def _prefers_owned_media(self, event: Dict[str, Any]) -> bool:
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            metadata = {}

        # Always prefer owned media for match events, predictions, and bo3gg
        if (bool(metadata.get('media_path'))
            or event.get('category') in ('match_result', 'match_preview', 'match_prediction')
            or event.get('source') == 'bo3gg'
            or bool(metadata.get('prefer_generated_media'))):
            return True

        # Also prefer for CS2 news articles that look like match results
        # (prevents wrong article OG images from being attached)
        if event.get('category') in ('cs2', 'match_highlight', 'analysis'):
            headline = (event.get('headline') or '')
            if any(p.search(headline) for p in self._RESULT_PATTERNS):
                return True

        return False

    # ── Fabrication Detection ─────────────────────────────────────

    # Regex patterns that indicate a factual match-result claim
    _RESULT_PATTERNS = [
        re.compile(p, re.IGNORECASE) for p in [
            r'\b(?:just|)\s*(?:beat|swept|eliminated|destroyed|upset|reverse.?swept|clutched|won|defeated|lost to|knocked out)\b',
            r'\b\d+\s*-\s*\d+\b',                     # score like "2-0", "16-13"
            r'\b(?:reverse sweep|3-0|3-1|3-2|2-0|2-1|0-3|0-2)\b',
            r'\bwon (?:the|a) (?:series|match|map|grand final|final|major|tournament)\b',
            r'\bmake (?:the|a) (?:major|playoffs|finals)\b',
            r'\b(?:eliminated|out of|knocked out of)\b.*\b(?:major|tournament|bucharest|cologne|katowice)\b',
            r'\b(?:carried|dropped)\b.*\b(?:final|series|match|major|tournament)\b',
            r'\b\d{2,}\s*(?:kills?|frags?)\b',         # "40 kills", "56 frags"
            r'\b(?:came back|comeback)\b.*\b(?:win|won|take)\b',
        ]
    ]

    def _detect_fabrication(self, tweet_text: str, event: Dict[str, Any]) -> bool:
        """
        Check if a synthetic tweet claims specific match results/outcomes
        that don't exist in our real events DB.
        
        Returns True if the tweet appears to fabricate factual claims.
        """
        # Only check synthetic sources — real RSS/HLTV events are grounded by definition
        if event.get('source') not in ('engagement_engine',):
            return False

        # Check if the tweet makes factual match-result claims
        has_result_claim = any(p.search(tweet_text) for p in self._RESULT_PATTERNS)
        if not has_result_claim:
            return False  # opinion/hype tweets are fine

        # The tweet claims a match result. Cross-reference against real events.
        try:
            with self.db_conn.cursor() as cur:
                # Get all real headlines from the last 48h (not from engagement_engine)
                cur.execute("""
                    SELECT headline FROM twitter_bot.events
                    WHERE source != 'engagement_engine'
                    AND created_at > NOW() - INTERVAL '48 hours'
                    AND headline IS NOT NULL
                """)
                real_headlines = [r[0].lower() for r in cur.fetchall()]
        except Exception as e:
            logger.warning(f"⚠️  Fabrication check DB query failed, BLOCKING as precaution: {e}")
            return True  # fail-closed: if we can't verify, block it

        if not real_headlines:
            logger.warning("⚠️  No real events in DB to verify claim — blocking synthetic result claim")
            return True

        # Extract team/player subjects from the tweet
        from processing.media_manager import get_media_manager
        mm = get_media_manager()
        tweet_teams = mm._find_teams_in_text(tweet_text)
        tweet_players = mm.find_players_in_text(tweet_text)
        subjects = set(tweet_teams + tweet_players)

        if not subjects:
            # Synthetic tweet claims a result but names no identifiable team/player.
            # This is inherently unverifiable — block it. Opinions without result claims
            # already pass the has_result_claim check above.
            logger.warning(f"🚫 Synthetic tweet claims result with no identifiable subjects — blocking")
            return True

        # STRICT VERIFICATION: For synthetic events claiming match results,
        # we need a real headline that mentions the SAME specific claim,
        # not just the same team name. Use semantic keyword overlap.
        tweet_lower = tweet_text.lower()
        
        # Extract the key claim words from the tweet (action verbs + context)
        claim_keywords = set()
        for subject in subjects:
            claim_keywords.add(subject)
        # Add action words that indicate what happened
        for word in re.findall(r'\b[a-z]{3,}\b', tweet_lower):
            if word in ('just', 'the', 'are', 'was', 'has', 'had', 'been', 'with',
                        'that', 'this', 'their', 'they', 'from', 'into', 'some',
                        'back', 'down', 'over', 'about', 'more', 'what', 'when'):
                continue
            claim_keywords.add(word)

        # A headline corroborates the claim if it shares at least 3 keywords
        # with the tweet AND contains at least one of the same subjects.
        best_overlap = 0
        best_headline = ""
        for headline in real_headlines:
            headline_words = set(re.findall(r'\b[a-z]{3,}\b', headline))
            # Must share at least one subject (team/player)
            has_subject = any(subj in headline for subj in subjects)
            if not has_subject:
                continue
            overlap = len(claim_keywords & headline_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_headline = headline

        # Require significant keyword overlap — just sharing a team name isn't enough.
        # A real headline about "Astralis qualify for playoffs" should NOT corroborate
        # a fabricated "Astralis 2-0'd their group at the Major".
        MIN_OVERLAP = 4
        if best_overlap < MIN_OVERLAP:
            logger.warning(
                f"🚫 Fabrication detected: synthetic tweet claims result about {subjects} "
                f"but best real headline overlap is only {best_overlap}/{MIN_OVERLAP} keywords "
                f"(best match: '{best_headline[:60]}')"
            )
            return True

        logger.info(
            f"✅ Fabrication check PASSED: {best_overlap} keyword overlap "
            f"with real headline '{best_headline[:60]}'"
        )
        return False

    # ── Prediction Tweet Formatting ───────────────────────────────

    _PREDICTION_TEMPLATES = [
        "Taking {pick} {market_tag}{emoji}\n\n{analysis}\n\n{conf}% win prob | {edge}% edge",
        "{pick} {market_short}is the play {emoji}\n\n{analysis}\n\n{conf}% win probability",
        "Liking {pick} here {emoji}\n\n{analysis}\n\nEdge: {edge}%",
        "Giving {pick} the edge {market_tag}{emoji}\n\n{analysis}",
        "Going with {pick} {market_tag}{emoji}\n\n{analysis}\n\n{conf}% chance",
        "{pick} looking strong {emoji}\n\n{analysis}\n\nEdge: {edge}%",
    ]

    def _format_prediction_tweet(self, event: Dict[str, Any]) -> Optional[str]:
        """Format a prediction event into a casual pick-style tweet.
        No LLM — deterministic template + structured data."""
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            return None

        pick = metadata.get('pick', '')
        opponent = metadata.get('team2', '')
        if not pick or not opponent:
            return None

        confidence = metadata.get('confidence', 'standard')
        win_prob = float(metadata.get('win_probability', 0) or 0)
        edge = float(metadata.get('edge_pct', 0) or 0)
        factors = metadata.get('factors') or []
        event_name = metadata.get('event', '')
        market_type = metadata.get('market_type', 'match_winner')

        conf_pct = int(win_prob * 100) if win_prob else 75

        # Market-specific phrasing
        _MARKET_TAGS = {
            'match_winner': f'over {opponent} ',
            'map_winner': '(map winner) ',
            'map_handicap': '(map handicap) ',
            'total_maps': '',            # pick is "Over 2.5" etc.
            'total_rounds': '',
            'round_handicap': '(round handicap) ',
            'over_under': '',
            'pistol_round': '(pistol round) ',
        }
        _MARKET_SHORTS = {
            'match_winner': 'ML ',
            'map_winner': '(map) ',
            'map_handicap': '(maps) ',
            'total_maps': '',
            'total_rounds': '',
            'round_handicap': '',
            'over_under': '',
            'pistol_round': '',
        }
        market_tag = _MARKET_TAGS.get(market_type, f'({market_type}) ' if market_type != 'match_winner' else '')
        market_short = _MARKET_SHORTS.get(market_type, '')

        # Build readable analysis lines from the raw factors
        analysis_lines = []
        for f in factors:
            line = self._humanize_factor(f, pick, opponent, metadata)
            if line:
                analysis_lines.append(line)
        if not analysis_lines:
            analysis_lines = [f"{conf_pct}% win probability, {edge:.1f}% edge"]

        analysis_text = '\n'.join(f"• {l}" for l in analysis_lines[:4])

        # Pick emoji based on confidence
        if confidence == 'strong':
            emojis = ['🔥', '💰', '📈', '🎯']
        else:
            emojis = ['👀', '📊', '🤔', '💭']

        import hashlib as _hl
        seed = int(_hl.md5(f"{pick}{opponent}".encode()).hexdigest()[:8], 16)
        emoji = emojis[seed % len(emojis)]
        template = self._PREDICTION_TEMPLATES[seed % len(self._PREDICTION_TEMPLATES)]

        tweet = template.format(
            pick=pick,
            opponent=opponent,
            emoji=emoji,
            analysis=analysis_text,
            conf=conf_pct,
            edge=f"{edge:.1f}" if edge else "?",
            market_tag=market_tag,
            market_short=market_short,
        )

        # Add event name if short enough
        if event_name and len(tweet) + len(event_name) + 3 < 270:
            tweet += f"\n\n{event_name}"

        return tweet.strip()

    @staticmethod
    def _humanize_factor(raw: str, pick: str, opponent: str, metadata: dict) -> Optional[str]:
        """Convert a raw model factor string into a readable tweet line."""
        import re as _re
        f = raw.strip()
        fl = f.lower()
        team_a = metadata.get('team_a', '')
        team_b = metadata.get('team_b', '')

        # Skip baselines / zero-impact factors
        if any(skip in fl for skip in (
            '+0.0%', 'baseline', 'skipped', 'adj disabled',
            'even)', 'stable)', '0 flags', 'no data',
        )):
            return None

        # Ranking: "B ranked #176, A unranked (edge 1.7%)"
        if fl.startswith(('a ranked', 'b ranked')) or 'ranked #' in fl:
            m = _re.findall(r'ranked #(\d+)', f)
            if len(m) >= 2:
                return f"Rankings: #{m[0]} vs #{m[1]}"
            elif m:
                unranked_side = 'A' if 'a unranked' in fl else 'B' if 'b unranked' in fl else ''
                ranked_team = team_b if fl.startswith('b ranked') else team_a
                return f"{ranked_team} ranked #{m[0]}, opponent unranked"
            return None

        # Form: "Form(SoS): A 34% vs B 59%"
        if fl.startswith('form'):
            m = _re.search(r'(\d+)%\s*vs\s*\w+\s*(\d+)%', f)
            if m:
                a_form, b_form = int(m.group(1)), int(m.group(2))
                better = pick if (a_form > b_form) == (pick == team_a) else opponent
                return f"Form: {team_a} {a_form}% vs {team_b} {b_form}%"
            return None

        # Glicko: "Glicko-2 [PRIOR]: 27% (MEDIUM, conf=63%)"
        if fl.startswith('glicko'):
            m = _re.search(r'(\d+)%\s*\((\w+)', f)
            if m:
                pct, level = m.group(1), m.group(2)
                return f"Elo model: {pct}% win chance ({level.lower()} confidence)"
            return None

        # Roster factors
        if fl.startswith('roster'):
            m = _re.search(r'adj=([+\-][\d.]+)%', f)
            if m:
                val = float(m.group(1))
                if abs(val) >= 1:
                    return f"Roster factor: {m.group(1)}%"
            return None

        # LAN/Online
        if fl.startswith('lan'):
            m = _re.search(r'(\w+)\s+([+\-][\d.]+)%', f)
            if m:
                team, val = m.group(1), float(m.group(2))
                if abs(val) >= 3:
                    direction = "disadvantage" if val < 0 else "advantage"
                    return f"LAN/Online {direction}: {abs(val):.1f}%"
            return None

        # Rematch / H2H
        if 'rematch' in fl or 'h2h' in fl:
            if 'no recent' in fl:
                return None
            return f.split('(')[0].strip()[:50]

        # Tilt
        if fl.startswith('tilt'):
            m = _re.search(r'([+\-][\d.]+)%', f)
            if m and abs(float(m.group(1))) >= 2:
                return f"Tilt factor: {m.group(1)}%"
            return None

        # Quantum summary
        if fl.startswith('quantum'):
            m = _re.search(r'(\d+) constructive.*?(\d+) destructive.*?amplification=([\d.]+)', f)
            if m:
                c, d, amp = m.group(1), m.group(2), m.group(3)
                return f"Signal alignment: {c} constructive, {d} destructive ({amp}x)"
            return None

        # Model agreement
        if fl.startswith('model agreement'):
            m = _re.search(r'S=([\d.]+)', f)
            if m:
                s = float(m.group(1))
                if s >= 0.5:
                    return "Models strongly agree"
                elif s >= 0.2:
                    return "Models moderately agree"
            return None

        return None

    def _generate_owned_media(
        self,
        event: Dict[str, Any],
        tweet_text: str,
        pillar: int,
        account_bucket: str,
    ) -> Optional[str]:
        """Attach pre-existing media or generate match/player cards.
        Text-only cards (hot take, multi-team) are disabled."""
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            metadata = {}

        pre_media = metadata.get('media_path')
        if pre_media:
            media_id = self.media_manager.upload_media(pre_media, account_bucket=account_bucket)
            if media_id:
                logger.info(f"🎨 Owned media attached: {media_id}")
                return media_id

        match_context = metadata.get('match_context') or {}

        if event.get('category') == 'match_result':
            mvp = match_context.get('mvp')
            mvp_rating = match_context.get('mvp_rating')
            map_scores = metadata.get('map_scores') or metadata.get('maps') or []

            card_path = self.meme_generator.generate_match_result_card(
                team1=metadata.get('team1', ''),
                team2=metadata.get('team2', ''),
                score1=int(metadata.get('score1', 0) or 0),
                score2=int(metadata.get('score2', 0) or 0),
                map_scores=map_scores if map_scores else None,
                event_name=metadata.get('event', ''),
                mvp_name=mvp,
                mvp_rating=mvp_rating,
            )
            if card_path:
                media_id = self.media_manager.upload_media(card_path, account_bucket=account_bucket)
                if media_id:
                    logger.info(f"🎨 Match result card attached: {media_id}")
                    return media_id

            card_path = self.meme_generator.generate_vs_card(
                metadata.get('team1', ''),
                metadata.get('team2', ''),
                str(metadata.get('score1', '')),
                str(metadata.get('score2', '')),
                metadata.get('event', ''),
            )
            if card_path:
                media_id = self.media_manager.upload_media(card_path, account_bucket=account_bucket)
                if media_id:
                    logger.info(f"🎨 VS card attached: {media_id}")
                    return media_id

            if mvp and match_context.get('player_stats'):
                try:
                    mvp_stats_raw = next(
                        (p for p in match_context['player_stats'] if p['name'] == mvp),
                        None,
                    )
                    if mvp_stats_raw:
                        stats_dict = {'Rating': mvp_stats_raw.get('rating', '?')}
                        if 'kd_diff' in mvp_stats_raw:
                            stats_dict['K-D'] = mvp_stats_raw['kd_diff']
                        if 'adr' in mvp_stats_raw:
                            stats_dict['ADR'] = mvp_stats_raw['adr']
                        card_path = self.meme_generator.generate_player_card(
                            player_name=mvp,
                            stats=stats_dict,
                            event_name=metadata.get('event'),
                        )
                        if card_path:
                            media_id = self.media_manager.upload_media(card_path, account_bucket=account_bucket)
                            if media_id:
                                logger.info(f"🎨 Player stat card attached: {media_id}")
                                return media_id
                except Exception:
                    pass

        if event.get('category') == 'match_preview':
            card_path = self.meme_generator.generate_vs_card(
                metadata.get('team1', ''),
                metadata.get('team2', ''),
                event_name=metadata.get('event', ''),
                map_name=metadata.get('map_name') or metadata.get('map'),
            )
            if card_path:
                media_id = self.media_manager.upload_media(card_path, account_bucket=account_bucket)
                if media_id:
                    logger.info(f"🎨 Match preview card attached: {media_id}")
                    return media_id

        # Prediction cards
        if event.get('category') == 'match_prediction':
            # Build humanized analysis lines for the card
            raw_factors = metadata.get('factors') or []
            card_analysis = []
            pick = metadata.get('pick', metadata.get('team1', ''))
            opp = metadata.get('team2', '')
            for f in raw_factors:
                line = self._humanize_factor(f, pick, opp, metadata)
                if line:
                    card_analysis.append(line)
            card_path = self.meme_generator.generate_prediction_card(
                pick_team=pick,
                opponent=opp,
                win_probability=float(metadata.get('win_probability', 0) or 0),
                edge_pct=float(metadata.get('edge_pct', 0) or 0),
                confidence=metadata.get('confidence', 'standard'),
                pick_odds=float(metadata.get('pick_odds', 0) or 0) or None,
                event_name=metadata.get('event', ''),
                key_factor=(raw_factors or [None])[0],
                market_type=metadata.get('market_type', 'match_winner'),
                analysis_lines=card_analysis[:3],
            )
            if card_path:
                media_id = self.media_manager.upload_media(card_path, account_bucket=account_bucket)
                if media_id:
                    logger.info(f"🎯 Prediction card attached: {media_id}")
                    return media_id
            # Fallback: generate a plain VS card
            card_path = self.meme_generator.generate_vs_card(
                metadata.get('team1', ''), metadata.get('team2', ''),
                event_name=metadata.get('event', ''),
            )
            if card_path:
                media_id = self.media_manager.upload_media(card_path, account_bucket=account_bucket)
                if media_id:
                    logger.info(f"🎯 Prediction VS fallback attached: {media_id}")
                    return media_id

        # Fallback: for CS2 news articles that look like match results,
        # try to extract team names from headline/tweet and generate a VS
        # card.  This prevents wrong OG images (e.g. MIBR pic on a LAG tweet)
        # from unrelated article thumbnails.
        if event.get('category') in ('cs2', 'match_highlight', 'analysis'):
            headline = (event.get('headline') or '') + ' ' + tweet_text
            has_result = any(p.search(headline) for p in self._RESULT_PATTERNS)
            if has_result or metadata.get('team1'):
                teams = []
                if metadata.get('team1'):
                    teams = [metadata['team1'], metadata.get('team2', '')]
                else:
                    teams = self.media_manager._find_teams_in_text(headline)
                if len(teams) >= 2 and teams[0] and teams[1]:
                    card_path = self.meme_generator.generate_vs_card(
                        teams[0], teams[1],
                        event_name=metadata.get('event', ''),
                    )
                    if card_path:
                        media_id = self.media_manager.upload_media(card_path, account_bucket=account_bucket)
                        if media_id:
                            logger.info(f"🎨 News VS card (fallback) attached: {media_id}")
                            return media_id

        return None
    
    async def classify_event_category(self, content: str) -> str:
        """Use LLM to classify a VIP tweet into the proper pillar category"""
        try:
            result = self.generator.client.generate(
                prompt=(
                    'Classify this CS2 tweet into exactly one category:\n\n'
                    f'Tweet: "{content[:500]}"\n\n'
                    'Categories:\n'
                    '- roster_change: Player signing, leaving, benching, team roster news\n'
                    '- match_result: About a specific match result or score\n'
                    '- regulation: About rules, bans, VAC, regulations\n'
                    '- financial: About prize money, org funding, sponsorship deals\n'
                    '- vip_engagement: General opinion, banter, or content (none of the above)\n\n'
                    'Reply with ONLY the category name.'
                ),
                tier='eco',
                temperature=0.1,
                max_tokens=20
            )
            cat = result['text'].strip().lower().replace(' ', '_')
            valid = {'roster_change', 'match_result', 'regulation', 'financial', 'vip_engagement'}
            return cat if cat in valid else 'vip_engagement'
        except Exception as e:
            logger.warning(f"⚠️  Classification failed: {e}")
            return 'vip_engagement'

    async def process_event(self, event_id: str) -> bool:
        """
        Process a pending event through the full pipeline
        
        Returns:
            True if successfully queued, False otherwise
        """
        logger.info(f"⚙️  Processing event {event_id}...")
        
        slot_consumed = False  # Track if slot was used (tweet created) vs needs freeing
        reserved_bucket = None
        try:
            # Fetch event
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT id, headline, content, category, urgency, metadata, siftly_event_id, source, source_url
                    FROM twitter_bot.events
                    WHERE id = %s AND status = 'pending'
                """, (event_id,))
                
                row = cur.fetchone()
                if not row:
                    logger.warning(f"⚠️  Event {event_id} not found or already processed")
                    return False
                
                event = {
                    'id': str(row[0]),
                    'headline': row[1],
                    'content': row[2],
                    'category': row[3],
                    'urgency': row[4],
                    'metadata': row[5],
                    'siftly_event_id': str(row[6]) if row[6] else None,
                    'source': row[7],
                    'source_url': row[8]
                }
            
            # Max 3 attempts per event — skip if we've already failed too many times
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT count(*) FROM twitter_bot.tweets_v2
                    WHERE event_id = %s AND status NOT IN ('posted', 'queued', 'hitl_pending')
                """, (event['id'],))
                fail_count = cur.fetchone()[0]
                if fail_count >= 3:
                    logger.warning(f"⏭️  Skipping event {event['id'][:8]} after {fail_count} failed attempts")
                    cur.execute("UPDATE twitter_bot.events SET status = 'skipped' WHERE id = %s", (event['id'],))
                    self.db_conn.commit()
                    return False
            
            # Pure CS2 sources — everything they publish is CS2 by definition
            PURE_CS2_SOURCES = {'hltv', 'dust2us', 'bo3gg', 'valve_cs2', '1pvfr', 'prediction_model'}
            is_pure_source = event.get('source') in PURE_CS2_SOURCES

            # Quick CS2 relevance check (skip for pure CS2 sources & known CS2 categories)
            text_lower = ((event.get('headline') or '') + ' ' + (event.get('content') or '')).lower()
            # Reject if clearly about another game (even pure sources could syndicate off-topic)
            is_other_game = any(k in text_lower for k in NON_CS2_KEYWORDS)
            if is_other_game:
                logger.info(f"⏭️  Skipping non-CS2 event: {event['headline'][:60]}")
                with self.db_conn.cursor() as cur:
                    cur.execute("UPDATE twitter_bot.events SET status = 'skipped' WHERE id = %s", (event['id'],))
                    self.db_conn.commit()
                return False

            # For mixed-feed sources, require a CS2 keyword signal
            if not is_pure_source:
                has_cs2_signal = is_cs2_relevant(text_lower)
                if event['category'] not in ('match_result', 'vip_engagement', 'cs2_update', 'roster_change') and not has_cs2_signal:
                    logger.info(f"⏭️  Skipping non-CS2 event: {event['headline'][:60]}")
                    with self.db_conn.cursor() as cur:
                        cur.execute("UPDATE twitter_bot.events SET status = 'skipped' WHERE id = %s", (event['id'],))
                        self.db_conn.commit()
                    return False
            
            # T1-only filter: skip T2/T3 team matches and minor event news
            # Gap-aware: if we haven't posted in >90 min, relax the filter to avoid dead air
            minutes_since_last_post = self._minutes_since_last_post()
            is_drought = minutes_since_last_post is not None and minutes_since_last_post > 90
            if not is_t1_content(event.get('headline', ''), event.get('content', ''),
                                 event['category'], event.get('metadata')):
                if is_drought:
                    logger.info(f"🔓 Relaxing T1 filter — {minutes_since_last_post:.0f}min since last post: {event['headline'][:60]}")
                else:
                    logger.info(f"⏭️  Skipping T2/T3 event: {event['headline'][:60]}")
                    with self.db_conn.cursor() as cur:
                        cur.execute("UPDATE twitter_bot.events SET status = 'skipped' WHERE id = %s", (event['id'],))
                        self.db_conn.commit()
                    return False
            
            # Smart category reclassification for VIP tweets
            if event['category'] == 'vip_engagement' and event.get('content'):
                classified = await self.classify_event_category(event['content'])
                if classified != 'vip_engagement':
                    event['category'] = classified
                    logger.info(f"🏷️  Reclassified VIP event → {classified}")

            # Determine pillar
            pillar_mapping = {
                'roster_change': 1,
                'match_result': 2,
                'regulation': 3,
                'financial': 4,
                'cs2_update': 1,
                'vip_engagement': 12,
                'engagement_poll': 13,
                'engagement_conversation': 14,
                'engagement_take': 15,
                'engagement_recycle': 15,
                'engagement_milestone': 1,
                'match_preview': 2,
                'community_disagreement': 16,
                'match_prediction': 17,
            }
            pillar = pillar_mapping.get(event['category'], 1)
            reply_target_hint = None
            quote_target_hint = None
            if event['category'] == 'community_disagreement' and isinstance(event.get('metadata'), dict):
                target_tweet_id = event['metadata'].get('tweet_id')
                if (event['metadata'].get('reply_mode') or '').lower() == 'quote':
                    quote_target_hint = target_tweet_id
                else:
                    reply_target_hint = target_tweet_id
            account_bucket = select_account_bucket(
                pillar=pillar,
                pillar_name=event['category'],
                reply_target_id=reply_target_hint,
                quote_tweet_id=quote_target_hint,
            )

            if not self.reserve_slot(account_bucket):
                logger.warning(f"⏸️  Event {event_id} held - {account_bucket} quota exhausted")
                return False
            reserved_bucket = account_bucket
            
            # ── PREDICTION FAST-PATH ────────────────────────────────────
            # Predictions are structured data — no LLM, no guardrails, auto-approve.
            if event['category'] == 'match_prediction':
                tweet_text = self._format_prediction_tweet(event)
                if not tweet_text:
                    logger.error(f"❌ Prediction formatting failed for {event_id}")
                    return False

                # Generate prediction card
                media_id = None
                try:
                    media_id = self._generate_owned_media(event, tweet_text, pillar, account_bucket)
                except Exception as e:
                    logger.warning(f"⚠️  Prediction card failed (non-fatal): {e}")

                # Store directly — auto-approved, no guardrails needed
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO twitter_bot.tweets_v2
                        (event_id, pillar, pillar_name, content, status, account_bucket,
                         fact_checked, tone_validated, generation_model,
                         media_path, auto_approved, is_thread, thread_tweets)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (
                        event['id'], pillar, 'match_prediction', tweet_text,
                        'queued', account_bucket,
                        True, True, 'prediction_template',
                        media_id, True, False, None,
                    ))
                    tweet_id = cur.fetchone()[0]
                    cur.execute(
                        "UPDATE twitter_bot.events SET status = 'scheduled', processed_at = NOW() WHERE id = %s",
                        (event['id'],),
                    )
                    self.db_conn.commit()

                slot_consumed = True
                logger.info(f"🎯 Prediction tweet {tweet_id} queued (auto-approved)")
                return True

            # Enrich news events with full article content for better tweet generation
            if pillar in (1, 2, 3) and event.get('source_url'):
                try:
                    page_data = await asyncio.to_thread(
                        self.media_manager.fetch_article_page, event['source_url']
                    )
                    article_text = page_data.get('text')
                    if article_text:
                        event['content'] = (event.get('content') or '') + '\n\nFULL ARTICLE:\n' + article_text
                        logger.info(f"📰 Enriched with {len(article_text)} chars from article")
                except Exception as e:
                    logger.warning(f"⚠️  Article enrichment failed (non-fatal): {e}")
            
            # Generate content (dual-agent writer's room or thread for updates)
            is_thread = False
            thread_tweets = None
            
            # Use thread generation for Valve CS2 updates with substantial content
            if event['category'] == 'cs2_update' and len(event.get('content', '')) > 300:
                generation_result = self.generator.generate_thread(event, pillar)
                if generation_result.get('is_thread'):
                    is_thread = True
                    thread_tweets = generation_result['thread_tweets']
                    tweet_text = generation_result['final_text']
                    logger.info(f"🧵 Thread generated: {len(thread_tweets)} tweets")
                else:
                    tweet_text = generation_result['final_text']
            elif event['category'] == 'match_result':
                # Post-match analysis for T1 matches
                analysis = await self.match_analyzer.analyze_match(event)
                if analysis:
                    generation_result = analysis
                    tweet_text = analysis['final_text']
                    if analysis.get('is_thread') and analysis.get('thread_tweets'):
                        is_thread = True
                        thread_tweets = analysis['thread_tweets']
                        logger.info(f"🔬 Match analysis thread: {len(thread_tweets)} tweets")
                    else:
                        logger.info(f"🔬 Match analysis: {tweet_text[:80]}")
                    if analysis.get('is_upset'):
                        logger.info("⚡ UPSET — boosting to auto-approve")
                else:
                    # Not T1-worthy, use standard generation
                    generation_result = self.generator.dual_agent_generate(event, pillar)
                    tweet_text = generation_result['final_text']
            else:
                generation_result = self.generator.dual_agent_generate(event, pillar)
                tweet_text = generation_result['final_text']
            
            # Post-processing: fix "@ Username" → "@Username" (LLM safety artifact)
            if not tweet_text:
                logger.error(f"❌ Content generation returned empty text for event {event_id}")
                return False
            tweet_text = re.sub(r'@ (\w)', r'@\1', tweet_text)

            # Safety net: reject any tweet that leaked LLM instructions
            _leak_markers = [
                'we need to', 'we need a', 'we must', 'we should',
                'let\'s craft', 'let\'s write', 'let\'s create', 'let\'s generate',
                'craft a tweet', 'write a tweet', 'generate a tweet', 'create a tweet',
                'generate one tweet', 'generate a', 'write a',
                'tweet 1:', 'tweet 2:', 'tweet 3:',
                'max 280', 'max 270', '280 char', '270 char',
                'separated by', 'one idea per tweet', 'provide result',
                'no explanation', 'just the tweet', 'output only',
                'here is the tweet', 'here\'s the tweet', 'here\'s a tweet',
                'your critique', 'your task', 'your job',
                'pillar context', 'event data', 'headline:',
                'format:', 'rules:', 'ensure no',
                'b2 english', 'simple words', 'short sentences',
                'need to identify', 'must be 60', 'mention that', 'add maybe',
                # Reasoning / chain-of-thought leaks
                'must identify', 'need to determine', 'let me think',
                'i should', 'first,', 'step 1', 'step 2',
                'my task', 'the prompt', 'the headline', 'the event:',
                'the content:', 'the category', 'the source',
                'i\'ll write', 'i\'ll craft', 'i\'ll generate',
                'i will write', 'i will craft', 'i will generate',
                'must be under', 'keep it under', 'stay under',
                'analyzing', 'identify the', 'determine the',
            ]
            tweet_lower = tweet_text.lower()
            if any(m in tweet_lower for m in _leak_markers):
                logger.error(f"❌ REJECTED: Tweet leaked LLM instructions: {tweet_text[:100]}")
                return False
            # Regex reasoning detector — catches structural chain-of-thought leaks
            # that the flat marker list might miss (e.g. "Must identify teams. Likely...")
            _reasoning_re = re.compile(
                r'(?:'
                r'^must\s+(?:identify|determine|find|check|verify|include|mention)'
                r'|^(?:need|trying|going) to (?:identify|determine|find|figure)'
                r'|^(?:first|okay|alright|so),?\s+(?:i|we|let)'
                r'|(?:the event|the headline|event data|pillar \d+)\s*[:.] '
                r')',
                re.IGNORECASE | re.MULTILINE
            )
            if _reasoning_re.search(tweet_text):
                logger.error(f"❌ REJECTED: Tweet contains reasoning/chain-of-thought: {tweet_text[:100]}")
                return False
            # Also check thread tweets
            if is_thread and thread_tweets:
                for i, tt in enumerate(thread_tweets):
                    if any(m in tt.lower() for m in _leak_markers):
                        logger.error(f"❌ REJECTED: Thread tweet {i} leaked instructions: {tt[:100]}")
                        return False
                    if _reasoning_re.search(tt):
                        logger.error(f"❌ REJECTED: Thread tweet {i} contains reasoning: {tt[:100]}")
                        return False
            
            logger.info(f"✍️  Generated: {tweet_text[:80]}...")

            # ── FABRICATION DETECTOR ──────────────────────────────────
            # Hard block for synthetic events (engagement_engine) that claim
            # specific match results, scores, or outcomes not in our DB.
            # This is the LAST LINE OF DEFENSE — prompt-level guardrails are
            # insufficient to prevent LLM hallucination.
            _synthetic_sources = ('engagement_engine',)
            if event.get('source') in _synthetic_sources:
                is_fabricated = self._detect_fabrication(tweet_text, event)
                if is_fabricated:
                    logger.error(f"🚫 FABRICATION BLOCKED: {tweet_text[:100]}")
                    with self.db_conn.cursor() as cur:
                        cur.execute("""
                            UPDATE twitter_bot.events SET status = 'rejected'
                            WHERE id = %s
                        """, (event_id,))
                        self.db_conn.commit()
                    if reserved_bucket:
                        free_account_slot(self.db_conn, reserved_bucket)
                    return False
            # Fast-path for engagement tweets: skip LLM-based guardrails
            # Polls (13) and conversation starters (14) are pure opinions — safe to skip.
            # Pillar 15 (style takes) generates claims about players/teams and MUST be fact-checked.
            # Pillar 12 (VIP replies) excluded — replies to real people must go through guardrails + HITL.
            if pillar in (13, 14):
                fact_result = {'valid': True, 'issues': []}
                tone_result = {'valid': True, 'tone_score': 8}
                mirofish_result = None
                logger.info(f"⚡ Skipping guardrails for engagement pillar {pillar}")
            else:
                fact_context = event
                if pillar == 16 and isinstance(event.get('metadata'), dict):
                    disagreement_meta = dict(event['metadata'])
                    evidence_lines = disagreement_meta.get('evidence_lines') or []
                    fact_context = dict(event)
                    fact_context['content'] = "\n".join(
                        part for part in [event.get('content') or '', *evidence_lines] if part
                    )
                    target_teams = disagreement_meta.get('target_teams') or []
                    if target_teams and not disagreement_meta.get('teams'):
                        disagreement_meta['teams'] = target_teams
                    if target_teams and not disagreement_meta.get('team1'):
                        disagreement_meta['team1'] = target_teams[0]
                    if len(target_teams) > 1 and not disagreement_meta.get('team2'):
                        disagreement_meta['team2'] = target_teams[1]
                    fact_context['metadata'] = disagreement_meta

                # For engagement_recycle, the event content is the LLM's own output —
                # fact-checking it against itself is circular. Inject recent real headlines
                # so the fact-checker has actual events to compare against.
                if event.get('category') == 'engagement_recycle':
                    try:
                        with self.db_conn.cursor() as cur:
                            cur.execute("""
                                SELECT headline FROM twitter_bot.events
                                WHERE status IN ('processed', 'scheduled', 'posted')
                                AND created_at > NOW() - INTERVAL '24 hours'
                                AND source != 'engagement_engine'
                                ORDER BY created_at DESC LIMIT 10
                            """)
                            real_headlines = [r[0] for r in cur.fetchall() if r[0]]
                        if real_headlines:
                            fact_context = dict(event)
                            fact_context['content'] = (
                                "RECENT REAL CS2 EVENTS:\n"
                                + "\n".join(f"- {h}" for h in real_headlines)
                                + "\n\nThe tweet MUST NOT claim events that are not in this list."
                            )
                            logger.info(f"📋 Injected {len(real_headlines)} real headlines for engagement_recycle fact-check")
                    except Exception as e:
                        logger.warning(f"⚠️  Could not inject headlines for fact-check: {e}")

                # Run guardrails in parallel
                fact_check_task = asyncio.to_thread(self.fact_checker.check, tweet_text, fact_context)
                tone_check_task = asyncio.to_thread(self.tone_validator.validate, tweet_text, pillar)
                
                try:
                    fact_result, tone_result = await asyncio.gather(fact_check_task, tone_check_task)
                except Exception as e:
                    logger.error(f"❌ Guardrail check crashed: {e}", exc_info=True)
                    return False
                
                # Check if high-risk pillar needs MiroFish
                mirofish_result = None
                if pillar in [3, 7]:  # Hot takes, drama
                    mirofish_result = await self.mirofish_guard.vibe_check(tweet_text, pillar)
                
                if mirofish_result and mirofish_result['veto']:
                    logger.warning(f"❌ MiroFish VETO: Risk={mirofish_result['risk_score']:.2f}")
                    
                    # Store vetoed tweet
                    with self.db_conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO twitter_bot.tweets_v2
                            (event_id, pillar, content, status, mirofish_ratio_risk, account_bucket)
                            VALUES (%s, %s, %s, 'mirofish_veto', %s, %s)
                        """, (
                            event['id'],
                            pillar,
                            tweet_text,
                            mirofish_result['risk_score'],
                            account_bucket,
                        ))
                        self.db_conn.commit()
                    
                    return False
            
            # Check fact and tone — failures go to HITL for human review instead of auto-rejecting
            guardrail_failed = False
            guardrail_issues = []
            if not fact_result['valid']:
                logger.warning(f"⚠️  Fact check FAILED (routing to HITL): {fact_result['issues']}")
                guardrail_failed = True
                guardrail_issues.append(f"Fact: {fact_result['issues']}")
            
            if not tone_result['valid']:
                logger.warning(f"⚠️  Tone validation FAILED (routing to HITL): {tone_result['issues']}")
                guardrail_failed = True
                guardrail_issues.append(f"Tone: {tone_result['issues']}")
            
            # Try to attach media (player images for matches, article images for news)
            media_id = None
            if event.get('category') == 'engagement_recycle':
                # engagement_recycle has no source_url — Google Images returns random photos.
                # But we CAN generate owned media (team cards, VS cards) from the tweet text.
                try:
                    media_id = self._generate_owned_media(event, tweet_text, pillar, account_bucket)
                    if media_id:
                        logger.info(f"📸 Generated owned media for engagement_recycle: {media_id}")
                    else:
                        logger.info("📝 No owned media match for engagement_recycle — posting text-only")
                except Exception as e:
                    logger.warning(f"⚠️  Owned media generation failed for engagement_recycle: {e}")
            else:
                prefer_owned_media = self._prefers_owned_media(event)
                try:
                    if prefer_owned_media:
                        media_id = self._generate_owned_media(event, tweet_text, pillar, account_bucket)
                    if not media_id:
                        media_id = self.media_manager.get_media_for_event(
                            event,
                            tweet_text=tweet_text,
                            account_bucket=account_bucket,
                        )
                    if media_id:
                        logger.info(f"📸 Media attached: {media_id}")
                except Exception as e:
                    logger.warning(f"⚠️  Media extraction failed (non-fatal): {e}")

            # Vision enrichment: if a Twitch screenshot was captured, analyze it
            # and inject real game state data (round, score, economy) into the tweet
            screenshot_path = event.get('_screenshot_path')
            if screenshot_path and event['category'] == 'match_result':
                try:
                    from processing.screenshot_analyzer import get_screenshot_analyzer
                    analyzer = get_screenshot_analyzer()
                    game_state = analyzer.analyze_screenshot(screenshot_path)
                    if 'error' not in game_state:
                        # Store vision data on metadata for future reference
                        if isinstance(event.get('metadata'), dict):
                            event['metadata']['vision_game_state'] = game_state
                        # Check for highlight moments
                        highlight = analyzer.detect_highlight_moment(game_state)
                        if highlight and highlight.get('urgency') == 'high':
                            narration = analyzer.generate_live_narration(game_state)
                            if narration and len(narration) > 20:
                                # Append live context to the tweet
                                combined = f"{tweet_text}\n\n📺 {narration}"
                                if len(combined) <= 280:
                                    tweet_text = combined
                                    logger.info(f"👁️ Vision enriched tweet with: {narration[:60]}...")
                except Exception as e:
                    logger.debug(f"⚠️  Vision enrichment skipped: {e}")

            # Fallback: Generate meme/stat card if no media found
            # Try structured cards first, then headline card as universal fallback
            if not media_id:
                category = event.get('category', '')
                metadata = event.get('metadata') or {}
                # Check if tweet mentions multiple teams (non-vs) — worth generating a card
                _fallback_teams = self.media_manager._find_teams_in_text(tweet_text)
                has_structured_data = (
                    category in ('match_result', 'match_preview')
                    or pillar == 3
                    or len(_fallback_teams) >= 2
                    or (isinstance(metadata, dict) and (metadata.get('team1') or metadata.get('media_path')))
                )
                if has_structured_data:
                    try:
                        media_id = self._generate_owned_media(event, tweet_text, pillar, account_bucket)
                    except Exception as e:
                        logger.warning(f"⚠️  Card generation failed (non-fatal): {e}")

            # Ultimate fallback: headline card — every tweet gets an image
            if not media_id:
                try:
                    _fb_teams = self.media_manager._find_teams_in_text(tweet_text)
                    _fb_meta = event.get('metadata') or {}
                    card_path = self.meme_generator.generate_headline_card(
                        headline=event.get('headline', ''),
                        tweet_text=tweet_text,
                        category=event.get('category', ''),
                        teams=_fb_teams if _fb_teams else None,
                        event_name=(_fb_meta.get('event', '') if isinstance(_fb_meta, dict) else ''),
                    )
                    if card_path:
                        media_id = self.media_manager.upload_media(card_path, account_bucket=account_bucket)
                        if media_id:
                            logger.info(f"📰 Headline card fallback attached: {media_id}")
                except Exception as e:
                    logger.warning(f"⚠️  Headline card fallback failed (non-fatal): {e}")
            
            # Auto-approve logic
            # The 3-agent writer's room + fact check + tone validation already vet content.
            # Only send to HITL for edge cases; auto-approve high-quality tweets.
            auto_approved = False
            tone_score = tone_result.get('tone_score', 0)
            fact_passed = fact_result.get('valid', False)
            
            # Tier 1: Factual news from trusted sources
            # Includes generic "cs2" category — hltv/dust2us often label things
            # "cs2" instead of fine-grained categories like "match_result".
            _trusted_sources = ('hltv', 'dust2us', 'valve_cs2')
            _news_categories = ('match_result', 'cs2_update', 'roster_change', 'cs2')
            if pillar in (1, 2) and event['category'] in _news_categories:
                if fact_passed and (event.get('source') in _trusted_sources or tone_score >= 7):
                    auto_approved = True
                    logger.info(f"🤖 Auto-approved: news (fact=✅, src={event.get('source')}, tone={tone_score}/10)")
            
            # Tier 2: Upsets — time-sensitive engagement magnets
            if generation_result.get('is_upset') and not auto_approved:
                auto_approved = True
                logger.info("⚡ Auto-approved: UPSET detected — time-sensitive")
            
            # Tier 3: Any tweet passing both checks with strong scores
            if not auto_approved and tone_score >= 8 and fact_passed:
                auto_approved = True
                logger.info(f"🤖 Auto-approved: high-quality (fact=✅, tone={tone_score}/10)")
            
            # Tier 4: Hot takes / engagement pillars with good tone (no hard facts to check)
            # NOTE: Pillar 12 (VIP replies) excluded — always requires HITL approval.
            # Pillar 15 (style takes) must pass fact check first — they claim player/team associations.
            if not auto_approved and pillar in (3, 13, 14) and tone_score >= 7:
                auto_approved = True
                logger.info(f"🤖 Auto-approved: engagement pillar {pillar} (tone={tone_score}/10)")
            
            if not auto_approved and pillar == 15 and tone_score >= 7 and fact_passed:
                auto_approved = True
                logger.info(f"🤖 Auto-approved: style take (fact=✅, tone={tone_score}/10)")

            # Pillar 16 (disagreement replies) always goes through HITL — replies to real
            # people's tweets are high-risk and hard to undo.
            
            # Force HITL for guardrail failures, VIP replies, or disagreement replies
            if guardrail_failed or pillar in (12, 16):
                auto_approved = False
                if pillar == 12:
                    logger.info("📱 VIP reply → forcing HITL review")
                elif pillar == 16:
                    logger.info("📱 Disagreement reply → forcing HITL review")
            
            status = 'queued' if auto_approved else 'hitl_pending'
            
            # For VIP replies, set reply_target_id or quote_tweet_id
            reply_target_id = None
            quote_tweet_id = None
            if pillar == 12 and isinstance(event.get('metadata'), dict):
                target_tweet_id = event['metadata'].get('tweet_id')
                # 80% quote tweets — replies get 0 impressions (94/94 pruned at 0 imp).
                # Quote tweets appear in our followers' feeds; replies don't.
                if target_tweet_id and random.random() < 0.80:
                    quote_tweet_id = target_tweet_id
                    logger.info("💬 VIP engagement → Quote Tweet mode")
                else:
                    reply_target_id = target_tweet_id
            elif pillar == 16 and isinstance(event.get('metadata'), dict):
                target_tweet_id = event['metadata'].get('tweet_id')
                reply_mode = (event['metadata'].get('reply_mode') or 'reply').lower()
                if target_tweet_id and reply_mode == 'quote':
                    quote_tweet_id = target_tweet_id
                    logger.info("💬 Community disagreement → Quote mode")
                else:
                    reply_target_id = target_tweet_id
            
            # Peak hour scheduling: stagger non-urgent tweets across 15:00–23:00 UTC
            scheduled_post_at = None
            now_utc = datetime.now(timezone.utc)
            if not auto_approved and event.get('urgency') != 'breaking':
                if now_utc.hour not in self.peak_hours:
                    # Pick a random slot within the peak window to avoid pile-ups
                    peak_hour = random.choice(self.peak_hours)
                    peak_minute = random.randint(0, 45)
                    next_peak = now_utc.replace(hour=peak_hour, minute=peak_minute, second=0, microsecond=0)
                    if next_peak <= now_utc:
                        next_peak += timedelta(days=1)
                    scheduled_post_at = next_peak
                    logger.info(f"⏰ Scheduled for peak hour: {next_peak.strftime('%H:%M UTC')}")
            
            # Strip any hashtags the LLM may have added (we don't use them)
            tweet_text = re.sub(r'\n*\s*#\S+', '', tweet_text).rstrip()

            # Store tweet
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO twitter_bot.tweets_v2
                    (event_id, pillar, pillar_name, content, status, account_bucket,
                     fact_checked, tone_validated, mirofish_ratio_risk,
                     generation_model, reply_target_id, quote_tweet_id,
                     media_path, auto_approved, scheduled_post_at,
                     is_thread, thread_tweets)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    event['id'],
                    pillar,
                    event['category'],
                    tweet_text,
                    status,
                    account_bucket,
                    True,
                    True,
                    mirofish_result['risk_score'] if mirofish_result else None,
                    generation_result.get('model', 'unknown'),
                    reply_target_id,
                    quote_tweet_id,
                    media_id,
                    auto_approved,
                    scheduled_post_at,
                    is_thread,
                    Json(thread_tweets) if thread_tweets else None
                ))
                
                tweet_id = cur.fetchone()[0]
                
                # Update event status
                cur.execute("""
                    UPDATE twitter_bot.events
                    SET status = 'scheduled', processed_at = NOW()
                    WHERE id = %s
                """, (event['id'],))
                
                self.db_conn.commit()
            
            slot_consumed = True  # Tweet created — slot is used, don't free it
            logger.info(f"✅ Tweet {tweet_id} created with status '{status}'")
            
            if status == 'hitl_pending':
                logger.info("📱 Sending to Telegram HITL gateway...")
                # Telegram bot will pick this up
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to process event: {e}", exc_info=True)
            try:
                self.db_conn.rollback()
            except Exception:
                pass
            return False
        finally:
            if not slot_consumed and reserved_bucket:
                self.free_slot(reserved_bucket)
    
    async def run_cycle(self):
        """Process pending events"""
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                # Fetch pending events (prioritize by urgency)
                # Event TTL: skip events older than 12 hours (stale news)
                cur.execute("""
                    SELECT id
                    FROM twitter_bot.events
                    WHERE status = 'pending'
                    AND created_at > NOW() - INTERVAL '12 hours'
                    ORDER BY 
                        CASE urgency
                            WHEN 'breaking' THEN 1
                            WHEN 'important' THEN 2
                            WHEN 'normal' THEN 3
                            ELSE 4
                        END,
                        created_at ASC
                    LIMIT 15
                """)
                
                pending_events = [str(row[0]) for row in cur.fetchall()]
            
            if not pending_events:
                logger.debug("ℹ️  No pending events")
                return
            
            logger.info(f"🔄 Processing {len(pending_events)} pending events...")
            
            consecutive_failures = 0
            for event_id in pending_events:
                success = await self.process_event(event_id)
                if success:
                    consecutive_failures = 0
                    await asyncio.sleep(2)  # Space out LLM calls
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        logger.warning("⏸️  3 consecutive failures (likely rate-limited) — backing off 5 min")
                        await asyncio.sleep(300)
                        consecutive_failures = 0
                    else:
                        await asyncio.sleep(30)  # Short backoff after single failure
            
        except Exception as e:
            logger.error(f"❌ Cycle failed: {e}")
    
    async def run_forever(self):
        """Main loop - runs every 5 minutes"""
        self.connect_db()
        logger.info("🚀 Tweet Scheduler started")
        
        while True:
            try:
                self._ensure_db()
                # Auto-expire stale pending events older than 12 hours
                try:
                    with self.db_conn.cursor() as cur:
                        cur.execute("""
                            UPDATE twitter_bot.events
                            SET status = 'expired'
                            WHERE status = 'pending'
                            AND created_at < NOW() - INTERVAL '12 hours'
                        """)
                        expired = cur.rowcount
                        
                        # Auto-expire stale HITL tweets older than 7 days (never reviewed)
                        cur.execute("""
                            UPDATE twitter_bot.tweets_v2
                            SET status = 'expired', updated_at = NOW()
                            WHERE status = 'hitl_pending'
                            AND created_at < NOW() - INTERVAL '7 days'
                        """)
                        hitl_expired = cur.rowcount
                        
                        self.db_conn.commit()
                        if expired:
                            logger.info(f"🗑️  Auto-expired {expired} stale events (>12h old)")
                        if hitl_expired:
                            logger.info(f"🗑️  Auto-expired {hitl_expired} stale HITL tweets (>7d old)")
                except Exception as e:
                    logger.warning(f"⚠️  Failed to expire stale items: {e}")
                
                await self.run_cycle()
            except Exception as e:
                logger.error(f"❌ Run cycle failed: {e}")
            
            # Run every 5 minutes
            await asyncio.sleep(300)
    
    def cleanup(self):
        """Cleanup resources"""
        if self.db_conn:
            self.db_conn.close()


async def main():
    scheduler = TweetScheduler()
    try:
        await scheduler.run_forever()
    except KeyboardInterrupt:
        logger.info("⏹️  Shutting down Tweet Scheduler...")
    finally:
        scheduler.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
