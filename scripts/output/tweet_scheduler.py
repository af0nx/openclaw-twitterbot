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
import signal
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple
import os

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
import json

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.content_generator import (
    ContentGenerator,
    is_invalid_tweet_candidate,
    normalize_generated_text,
)
from processing.fact_checker import get_fact_checker
from processing.tone_validator import get_tone_validator
from processing.mirofish_guard import get_mirofish_guard
from processing.media_manager import get_media_manager
from processing.meme_generator import get_meme_generator
from processing.hashtag_injector import inject_hashtags, inject_hashtags_thread
from processing.match_analyzer import get_match_analyzer
from processing.hybrid_prediction_pricer import get_hybrid_prediction_pricer
from processing.team_rating_engine import get_team_rating_engine
from utils.account_quota import (
    free_account_slot,
    reconcile_account_reservations,
    reserve_account_slot,
)
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
        self.hybrid_pricer = get_hybrid_prediction_pricer()
        self.rating_engine = get_team_rating_engine()
        self._schema_ready = False
        
        self.daily_cap = int(os.getenv('DAILY_TWEET_CAP', 95))
        self.dry_run = os.getenv('DRY_RUN_MODE', 'false').lower() == 'true'
        self.review_only = os.getenv('DASHBOARD_REVIEW_ONLY', 'false').lower() in ('1', 'true', 'yes', 'on')
        
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
            self.hybrid_pricer.connect_db(self.db_conn)
            self.rating_engine.connect_db(self.db_conn)
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise

    def _ensure_db(self):
        """Lightweight reconnect guard — call before every DB operation"""
        self.db_conn = ensure_db_connection(self.db_conn)
        self.hybrid_pricer.connect_db(self.db_conn)
        self.rating_engine.connect_db(self.db_conn)

    def mark_event_processed(self, event_id: str, status: str, reason: str) -> None:
        """Mark a filtered event as processed so pipeline metrics reflect scheduler decisions."""
        try:
            with self.db_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE twitter_bot.events
                    SET status = %s,
                        processed_at = NOW(),
                        metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                    WHERE id = %s
                    """,
                    (status, Json({'processing_reason': reason}), event_id),
                )
                self.db_conn.commit()
        except Exception:
            self.db_conn.rollback()
            raise
    
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

    def reconcile_reserved_slots(self):
        """Repair anonymous quota reservations left behind by killed workers."""
        try:
            result = reconcile_account_reservations(self.db_conn)
            changed = [
                f"{bucket}:{data['previous_reserved']}->{data['writes_reserved']}"
                for bucket, data in result.items()
                if data.get('changed')
            ]
            if changed:
                logger.warning("🧮 Reconciled account reservations: %s", ", ".join(changed))
            else:
                logger.info("🧮 Account reservations already match open tweets")
        except Exception as e:
            logger.warning(f"⚠️  Failed to reconcile account reservations: {e}")

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

    @staticmethod
    def _extract_matchup_teams(metadata: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        if not isinstance(metadata, dict):
            return None, None
        if metadata.get('team_a') and metadata.get('team_b'):
            return metadata.get('team_a'), metadata.get('team_b')
        teams = metadata.get('teams')
        if isinstance(teams, list) and len(teams) >= 2:
            return teams[0], teams[1]
        return metadata.get('team1'), metadata.get('team2')

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None or value == '':
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_preview_path(candidate: Any) -> Optional[str]:
        if not candidate:
            return None

        raw_path = str(candidate).strip()
        if not raw_path or raw_path.isdigit() or re.match(r'^https?://', raw_path, re.IGNORECASE):
            return None

        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (Path(__file__).resolve().parents[2] / path).resolve()
        else:
            path = path.resolve()

        if not path.exists() or not path.is_file():
            return None

        return str(path)

    def _build_media_attachment(
        self,
        media_ref: Optional[str] = None,
        preview_path: Optional[str] = None,
    ) -> Optional[Dict[str, Optional[str]]]:
        normalized_preview = self._normalize_preview_path(preview_path)
        if not media_ref and not normalized_preview:
            return None
        return {
            'media_ref': media_ref,
            'preview_path': normalized_preview,
        }

    def _dashboard_review_status(self, default_status: str) -> str:
        return 'draft' if self.review_only else default_status

    def _dashboard_preview_hint(self, event: Dict[str, Any]) -> Optional[str]:
        metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
        preview_candidates = [
            metadata.get('media_path') if isinstance(metadata, dict) else None,
            event.get('_screenshot_path'),
            event.get('_external_media_preview_path'),
        ]
        for candidate in preview_candidates:
            preview_path = self._normalize_preview_path(candidate)
            if preview_path:
                return preview_path
        return None

    @staticmethod
    def _explicit_visual_requested(event: Dict[str, Any]) -> bool:
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            return False
        return bool(
            metadata.get('prefer_generated_media') in (True, 'true', 'required', 'premium_result')
            or metadata.get('media_path')
            or isinstance(metadata.get('signal_card'), dict)
            or isinstance(metadata.get('race_watch'), dict)
            or isinstance(metadata.get('scenario_tree'), dict)
            or isinstance(metadata.get('player_snapshot'), dict)
        )

    @staticmethod
    def _is_low_structure_news_take(event: Dict[str, Any]) -> bool:
        if event.get('category') != 'cs2':
            return False
        if TweetScheduler._explicit_visual_requested(event):
            return False

        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            metadata = {}
        structured_keys = (
            'team1', 'team2', 'winner', 'score1', 'score2',
            'map_scores', 'maps', 'match_context',
        )
        if any(metadata.get(key) for key in structured_keys):
            return False

        text = ' '.join(str(part or '') for part in (event.get('headline'), event.get('content')))
        result_patterns = [
            r'\b\d+\s*-\s*\d+\b',
            r'\b(?:defeated|swept|eliminated|destroyed|upset|reverse.?swept|clutched|won|lost to|knocked out)\b',
            r'(?<!to )\bbeat\b',
            r'\b(?:3-0|3-1|3-2|2-0|2-1|0-3|0-2)\b',
        ]
        return not any(re.search(pattern, text, re.IGNORECASE) for pattern in result_patterns)

    @staticmethod
    def _allows_text_only_main_feed(event: Dict[str, Any]) -> bool:
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            return False

        if TweetScheduler._explicit_visual_requested(event):
            return False

        media_mode = str(metadata.get('media_mode') or metadata.get('media_policy') or '').strip().lower()
        return bool(
            metadata.get('allow_text_only')
            or metadata.get('text_only_ok')
            or media_mode in ('text_only', 'text-only', 'no_media', 'no-media', 'none')
            or TweetScheduler._is_low_structure_news_take(event)
        )

    @staticmethod
    def _requires_main_page_media(event: Dict[str, Any], pillar: Optional[int] = None) -> bool:
        """Main-feed posts must carry a visual. Replies and quote-replies are exempt."""
        if TweetScheduler._allows_text_only_main_feed(event):
            return False

        category = event.get('category') or ''
        if category in ('vip_engagement', 'community_disagreement'):
            return False
        if pillar in (12, 16):
            return False
        return True

    @staticmethod
    def _apply_pricing_context(
        metadata: Dict[str, Any],
        pricing_context: Dict[str, Any],
        rating_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        metadata['pricing_context'] = pricing_context
        if rating_context:
            metadata['rating_context'] = rating_context
        metadata['internal_win_probability'] = pricing_context.get('hybrid_win_probability')
        metadata['internal_edge_pct'] = pricing_context.get('signed_edge_pct')
        metadata['internal_fair_odds'] = pricing_context.get('fair_odds')
        if pricing_context.get('hybrid_win_probability') is not None:
            metadata['win_probability'] = pricing_context.get('hybrid_win_probability')
        if pricing_context.get('display_edge_pct') is not None:
            metadata['edge_pct'] = pricing_context.get('display_edge_pct')
        return metadata

    def _inject_pricing_context(self, event: Dict[str, Any]):
        if event.get('category') != 'match_prediction':
            return

        metadata = event.get('metadata')
        if not isinstance(metadata, dict):
            return

        existing_pricing = metadata.get('pricing_context')
        if isinstance(existing_pricing, dict) and existing_pricing.get('hybrid_win_probability') is not None:
            rating_context = metadata.get('rating_context') if isinstance(metadata.get('rating_context'), dict) else None
            event['metadata'] = self._apply_pricing_context(metadata, existing_pricing, rating_context)
            return

        team_a, team_b = self._extract_matchup_teams(metadata)
        pick = metadata.get('pick')
        if not team_a or not team_b or not pick:
            return

        provider_win_probability = metadata.get('provider_win_probability')
        if provider_win_probability is None:
            provider_win_probability = metadata.get('win_probability')

        try:
            pricing_context = self.hybrid_pricer.price_prediction(
                team_a=team_a,
                team_b=team_b,
                pick_team=pick,
                market_type=metadata.get('market_type', 'match_winner'),
                pick_odds=metadata.get('pick_odds'),
                provider_win_probability=provider_win_probability,
                analytics_context=metadata.get('analytics_context') if isinstance(metadata.get('analytics_context'), dict) else None,
                event_name=metadata.get('event') or metadata.get('event_name') or '',
            )
        except Exception as exc:
            logger.warning(f"⚠️  Hybrid pricing unavailable for {team_a} vs {team_b}: {exc}")
            return

        if not pricing_context:
            return

        rating_context = pricing_context.pop('rating_context', None)
        if 'provider_win_probability' not in metadata:
            metadata['provider_win_probability'] = self._safe_float(provider_win_probability, 0.0)
        if 'provider_edge_pct' not in metadata:
            metadata['provider_edge_pct'] = self._safe_float(metadata.get('edge_pct'), 0.0)
        event['metadata'] = self._apply_pricing_context(metadata, pricing_context, rating_context)

    def _inject_rating_context(self, event: Dict[str, Any]):
        metadata = event.get('metadata')
        if not isinstance(metadata, dict):
            return

        team_a, team_b = self._extract_matchup_teams(metadata)
        if not team_a or not team_b:
            return

        try:
            rating_context = self.rating_engine.get_matchup_context(
                team_a=team_a,
                team_b=team_b,
                event_name=metadata.get('event') or metadata.get('event_name') or '',
                pick_team=metadata.get('pick'),
            )
        except Exception as exc:
            logger.warning(f"⚠️  Team ratings unavailable for {team_a} vs {team_b}: {exc}")
            return

        if not rating_context:
            return

        metadata['rating_context'] = rating_context
        if event.get('category') in ('analysis', 'match_preview', 'match_result'):
            evidence_lines = [
                str(line).strip()
                for line in (metadata.get('evidence_lines') or [])
                if str(line).strip()
            ]
            for line in reversed(rating_context.get('evidence_lines') or []):
                if line and line not in evidence_lines:
                    evidence_lines.insert(0, line)
            metadata['evidence_lines'] = evidence_lines[:6]
        event['metadata'] = metadata

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

    @staticmethod
    def _requires_premium_result_media(event: Dict[str, Any]) -> bool:
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            return False
        return (
            event.get('category') == 'match_result'
            and metadata.get('prefer_generated_media') == 'premium_result'
            and bool(metadata.get('team1'))
            and bool(metadata.get('team2'))
        )

    @staticmethod
    def _media_plan_blocks_fallback(media_plan: Dict[str, Any]) -> bool:
        return bool(media_plan.get('force_text_only'))

    def _owned_media_enabled_types(self) -> set[str]:
        raw = os.getenv('OWNED_MEDIA_TYPES', 'match_result,match_preview,prediction,player,analysis')
        return {item.strip().lower() for item in raw.split(',') if item.strip()}

    def _determine_owned_media_group(self, event: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        category = event.get('category') or ''
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            metadata = {}

        if category == 'match_result':
            if self._requires_premium_result_media(event):
                return 'match_result', 'market_result_card'
            return 'match_result', 'match_result_card'
        if category == 'match_preview':
            return 'match_preview', 'match_preview_card'
        if category == 'match_prediction':
            return 'prediction', 'prediction_card'
        if category == 'engagement_recycle':
            return 'match_result', 'recycle_match_card'
        if category in ('cs2', 'match_highlight', 'analysis') and self._prefers_owned_media(event):
            return 'match_preview', 'headline_vs_card'
        if metadata.get('media_path'):
            return 'match_preview', 'prebuilt_owned_media'
        if self._requires_main_page_media(event):
            return 'analysis', 'main_page_card'
        return None, None

    def _get_media_experiment_stats(
        self,
        category: str,
        card_type: str,
        lookback_days: int = 30,
    ) -> Dict[str, Dict[str, Any]]:
        self._ensure_db()
        stats: Dict[str, Dict[str, Any]] = {}
        try:
            with self.db_conn.cursor() as cur:
                try:
                    cur.execute(
                        """
                        SELECT variant, avg_er, tweet_count
                        FROM twitter_bot.mv_media_experiment_30d
                        WHERE category = %s AND card_type = %s
                        """,
                        (category, card_type),
                    )
                    rows = cur.fetchall()
                except Exception:
                    self.db_conn.rollback()
                    cur.execute(
                        """
                        SELECT COALESCE(e.metadata->'media_experiment'->>'variant', 'unknown') AS variant,
                               AVG(t.engagement_rate) AS avg_er,
                               COUNT(*) AS tweet_count
                        FROM twitter_bot.tweets_v2 t
                        JOIN twitter_bot.events e ON e.id = t.event_id
                        WHERE e.category = %s
                          AND COALESCE(e.metadata->'media_experiment'->>'card_type', 'unknown') = %s
                          AND t.status = 'posted'
                          AND t.engagement_rate IS NOT NULL
                          AND t.posted_at > NOW() - (%s || ' days')::interval
                        GROUP BY 1
                        """,
                        (category, card_type, str(lookback_days)),
                    )
                    rows = cur.fetchall()

            for variant, avg_er, tweet_count in rows:
                stats[str(variant or 'unknown')] = {
                    'avg_er': float(avg_er) if avg_er is not None else None,
                    'sample_count': int(tweet_count or 0),
                }
        except Exception as e:
            logger.warning(f"⚠️  Media experiment stats lookup failed: {e}")

        return stats

    def _choose_owned_media_variant(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if self._allows_text_only_main_feed(event):
            return {
                'eligible': True,
                'allow_owned_media': False,
                'force_text_only': True,
                'metadata_patch': {
                    'media_mode': 'text_only',
                    'allow_text_only': True,
                    'media_experiment': {
                        'card_type': 'text_only_main_feed',
                        'variant': 'text_only_requested',
                        'policy': 'text_only',
                        'assigned_at': datetime.now(timezone.utc).isoformat(),
                    }
                },
            }

        enabled_types = self._owned_media_enabled_types()
        media_group, card_type = self._determine_owned_media_group(event)
        if not media_group or not card_type:
            return {
                'eligible': False,
                'allow_owned_media': False,
                'force_text_only': False,
            }

        requires_main_media = self._requires_main_page_media(event)
        if ('none' in enabled_types or media_group not in enabled_types) and not requires_main_media:
            return {
                'eligible': True,
                'allow_owned_media': False,
                'force_text_only': True,
                'metadata_patch': {
                    'media_experiment': {
                        'card_type': card_type,
                        'variant': 'text_only_disabled',
                        'policy': 'disabled',
                        'assigned_at': datetime.now(timezone.utc).isoformat(),
                    }
                },
            }

        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            metadata = {}

        requires_premium_media = self._requires_premium_result_media(event)
        explicitly_required_media = metadata.get('prefer_generated_media') == 'required'
        if requires_premium_media or explicitly_required_media or requires_main_media:
            return {
                'eligible': True,
                'allow_owned_media': True,
                'force_text_only': False,
                'metadata_patch': {
                    'media_experiment': {
                        'card_type': card_type,
                        'variant': 'owned_media',
                        'policy': 'required_main_page_media' if (
                            requires_main_media and not (requires_premium_media or explicitly_required_media)
                        ) else 'required',
                        'assigned_at': datetime.now(timezone.utc).isoformat(),
                    }
                },
            }

        policy = os.getenv('OWNED_MEDIA_POLICY', 'experiment').strip().lower()
        exploration_rate = float(os.getenv('OWNED_MEDIA_EXPLORATION_RATE', '0.15'))
        min_samples = int(os.getenv('OWNED_MEDIA_MIN_SAMPLES', '5'))
        min_lift = float(os.getenv('OWNED_MEDIA_MIN_LIFT', '0.10'))
        category = event.get('category') or 'unknown'

        variant = 'owned_media'
        policy_used = policy
        stats = self._get_media_experiment_stats(category, card_type)
        owned_stats = stats.get('owned_media', {})
        text_stats = stats.get('text_only_holdout', {})
        owned_avg = owned_stats.get('avg_er')
        text_avg = text_stats.get('avg_er')
        owned_n = int(owned_stats.get('sample_count', 0) or 0)
        text_n = int(text_stats.get('sample_count', 0) or 0)

        if policy == 'text_only':
            variant = 'text_only_holdout'
        elif policy != 'always':
            if (
                owned_n >= min_samples
                and text_n >= min_samples
                and owned_avg is not None
                and text_avg is not None
                and owned_avg >= (text_avg * (1.0 + min_lift))
            ):
                policy_used = 'proven_uplift'
                variant = 'owned_media' if random.random() >= exploration_rate else 'text_only_holdout'
            else:
                policy_used = 'experiment'
                variant = 'owned_media' if random.random() < exploration_rate else 'text_only_holdout'

        metadata_patch = {
            'media_experiment': {
                'card_type': card_type,
                'variant': variant,
                'policy': policy_used,
                'owned_avg_er': owned_avg,
                'text_avg_er': text_avg,
                'owned_samples': owned_n,
                'text_samples': text_n,
                'assigned_at': datetime.now(timezone.utc).isoformat(),
            }
        }
        return {
            'eligible': True,
            'allow_owned_media': variant == 'owned_media',
            'force_text_only': variant != 'owned_media',
            'metadata_patch': metadata_patch,
        }

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

    @staticmethod
    def _clean_prediction_reason(metadata: Dict[str, Any], pick: str, opponent: str) -> str:
        """Turn model/debug wording into one short fan-readable reason."""
        candidates: List[str] = []
        reasoning_summary = metadata.get('reasoning_summary')
        if reasoning_summary:
            candidates.append(str(reasoning_summary))
        pricing_context = metadata.get('pricing_context') or {}
        if isinstance(pricing_context, dict):
            for key in ('provider_line', 'analytics_line', 'pick_line', 'tweet_line'):
                value = pricing_context.get(key)
                if value:
                    candidates.append(str(value))
        factors = metadata.get('factors') or []
        candidates.extend(str(factor) for factor in factors if factor)

        joined = ' '.join(candidates)
        lower = joined.lower()
        if not joined:
            return ''
        if 'ranking' in lower and 'form' in lower:
            return f"rankings and recent form both point to {pick}"
        if 'ranking' in lower:
            return f"the ranking gap favors {pick}"
        if 'form' in lower:
            return f"recent form leans toward {pick}"
        if 'model' in lower and ('agree' in lower or 'probability' in lower):
            return "the model numbers are on the same side"
        if 'value' in lower or 'edge' in lower:
            return "the price leaves enough value"

        cleaned = re.sub(r'\s+', ' ', joined)
        cleaned = cleaned.encode('ascii', 'ignore').decode('ascii').strip(' .')
        cleaned = re.sub(r'\b[A-Z]\s+\d+%\s+vs\s+[A-Z]\s+\d+%\b', '', cleaned).strip(' .')
        return cleaned[:95]

    def _format_prediction_tweet(self, event: Dict[str, Any]) -> Optional[str]:
        """Format a prediction event into simple CS fan language.
        No LLM: this keeps external model picks consistent and predictable."""
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            return None

        pick = metadata.get('pick', '')
        opponent = metadata.get('team2', '')
        if not pick or not opponent:
            return None

        def _num(value: Any) -> float:
            try:
                if value is None or value == '':
                    return 0.0
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        win_prob = _num(metadata.get('win_probability'))
        win_prob_pct = _num(metadata.get('win_probability_pct'))
        if win_prob_pct <= 0 and win_prob:
            win_prob_pct = win_prob if win_prob > 1 else win_prob * 100.0
        conf_pct = int(round(win_prob_pct)) if win_prob_pct else 0

        edge = _num(metadata.get('edge_pct'))
        pick_odds = _num(metadata.get('pick_odds') or metadata.get('market_odds'))
        event_name = metadata.get('event', '')
        market_type = metadata.get('market_type', 'match_winner')
        reason = self._clean_prediction_reason(metadata, pick, opponent)

        if market_type == 'match_winner':
            opener = f"I like {pick} over {opponent}."
        else:
            label = market_type.replace('_', ' ')
            opener = f"I like {pick} for {label}."

        lines = [opener]
        if conf_pct:
            lines.append(f"Model has it around {conf_pct}%.")
        if pick_odds > 1.01 and edge >= 1:
            lines.append(f"Price is {pick_odds:.2f}, edge {edge:.1f}%.")
        elif pick_odds > 1.01:
            lines.append(f"Price is {pick_odds:.2f}.")
        elif edge >= 1:
            lines.append(f"Edge is {edge:.1f}%.")
        if reason:
            lines.append(f"Main reason: {reason}.")

        optional_lines = []
        scheduled_at = metadata.get('scheduled_at')
        if scheduled_at:
            try:
                scheduled_dt = datetime.fromisoformat(str(scheduled_at).replace('Z', '+00:00'))
                optional_lines.append(scheduled_dt.astimezone(timezone.utc).strftime('%H:%M UTC'))
            except ValueError:
                pass
        if event_name and str(event_name).lower() not in {'cs2 match', 'external prediction'}:
            optional_lines.insert(0, str(event_name))
        if optional_lines:
            lines.append(' | '.join(optional_lines[:2]))
        lines.append("No lock. Just the lean.")

        while lines and len('\n'.join(lines)) > 280:
            optional_text = ' | '.join(optional_lines[:2])
            if optional_text and optional_text in lines:
                lines.remove(optional_text)
            elif "No lock. Just the lean." in lines:
                lines.remove("No lock. Just the lean.")
            elif reason and f"Main reason: {reason}." in lines:
                lines.remove(f"Main reason: {reason}.")
            else:
                lines.pop()

        return '\n'.join(lines).strip()

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
    ) -> Optional[Dict[str, Optional[str]]]:
        """Attach pre-existing media or generate match/player cards.
        Text-only cards (hot take, multi-team) are disabled.

        Enabled card types are controlled via OWNED_MEDIA_TYPES env var
        (comma-separated list; default = match_result,match_preview,prediction,player).
        Set OWNED_MEDIA_TYPES=none to go fully text-only safely.
        """
        _enabled_raw = os.getenv('OWNED_MEDIA_TYPES', 'match_result,match_preview,prediction,player,analysis')
        _enabled_types = {t.strip().lower() for t in _enabled_raw.split(',') if t.strip()}
        if 'none' in _enabled_types:
            return None

        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            metadata = {}

        pre_media = metadata.get('media_path')
        if pre_media:
            media_id = self.media_manager.upload_media(pre_media, account_bucket=account_bucket)
            if media_id:
                logger.info(f"🎨 Owned media attached: {media_id}")
                return self._build_media_attachment(media_id, pre_media)

        match_context = metadata.get('match_context') or {}

        if event.get('category') == 'match_result' and 'match_result' in _enabled_types:
            if self._requires_premium_result_media(event):
                score1 = str(metadata.get('score1', '')).strip()
                score2 = str(metadata.get('score2', '')).strip()
                score_text = f"{score1}-{score2}" if score1 and score2 else ''
                team1 = str(metadata.get('team1', '')).strip()
                team2 = str(metadata.get('team2', '')).strip()
                winner = str(metadata.get('winner') or team1).strip()
                opponent = team2 if winner.casefold() == team1.casefold() else team1
                card_path = self.meme_generator.generate_market_result_card(
                    winner_team=winner,
                    opponent=opponent,
                    score=score_text,
                    event_name=metadata.get('event', ''),
                    market_type=metadata.get('market_type', 'match_winner'),
                    win_probability=metadata.get('win_probability') or metadata.get('provider_win_probability'),
                    pick_odds=metadata.get('pick_odds'),
                )
                if card_path:
                    media_id = self.media_manager.upload_media(card_path, account_bucket=account_bucket)
                    if media_id:
                        logger.info(f"🎨 Premium result card attached: {media_id}")
                        return self._build_media_attachment(media_id, card_path)

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
                    return self._build_media_attachment(media_id, card_path)

            if 'match_preview' in _enabled_types:
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
                    return self._build_media_attachment(media_id, card_path)

            if mvp and match_context.get('player_stats') and 'player' in _enabled_types:
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
                                return self._build_media_attachment(media_id, card_path)
                except Exception:
                    pass

        if event.get('category') == 'match_preview' and 'match_preview' in _enabled_types:
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
                    return self._build_media_attachment(media_id, card_path)

        # Prediction cards
        if event.get('category') == 'match_prediction' and 'prediction' in _enabled_types:
            # Build humanized analysis lines for the card
            raw_factors = metadata.get('factors') or []
            card_analysis = []
            pick = metadata.get('pick', metadata.get('team1', ''))
            opp = metadata.get('team2', '')
            pricing_context = metadata.get('pricing_context') or {}
            if isinstance(pricing_context, dict):
                pricing_line = pricing_context.get('pick_line') or pricing_context.get('tweet_line')
                provider_line = pricing_context.get('provider_line')
                if pricing_line:
                    card_analysis.append(pricing_line)
                if provider_line and provider_line not in card_analysis:
                    card_analysis.append(provider_line)
            rating_context = metadata.get('rating_context') or {}
            if isinstance(rating_context, dict):
                rating_line = rating_context.get('pick_line') or rating_context.get('tweet_line')
                if rating_line and rating_line not in card_analysis:
                    card_analysis.append(rating_line)
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
                    return self._build_media_attachment(media_id, card_path)
            # Fallback: generate a plain VS card
            card_path = self.meme_generator.generate_vs_card(
                metadata.get('team1', ''), metadata.get('team2', ''),
                event_name=metadata.get('event', ''),
            )
            if card_path:
                media_id = self.media_manager.upload_media(card_path, account_bucket=account_bucket)
                if media_id:
                    logger.info(f"🎯 Prediction VS fallback attached: {media_id}")
                    return self._build_media_attachment(media_id, card_path)

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
                            return self._build_media_attachment(media_id, card_path)

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
                    self.mark_event_processed(event['id'], 'skipped', 'failed_attempts')
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
                self.mark_event_processed(event['id'], 'skipped', 'non_cs2_keyword')
                return False

            # For mixed-feed sources, require a CS2 keyword signal
            if not is_pure_source:
                has_cs2_signal = is_cs2_relevant(text_lower)
                if event['category'] == 'vip_engagement' and not has_cs2_signal:
                    logger.info(f"⏭️  Skipping non-CS2 VIP event: {event['headline'][:60]}")
                    self.mark_event_processed(event['id'], 'skipped', 'non_cs2_vip')
                    return False
                if event['category'] not in ('match_result', 'cs2_update', 'roster_change') and not has_cs2_signal:
                    logger.info(f"⏭️  Skipping non-CS2 event: {event['headline'][:60]}")
                    self.mark_event_processed(event['id'], 'skipped', 'non_cs2_relevance')
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
                    self.mark_event_processed(event['id'], 'skipped', 'non_t1_content')
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

            self._inject_pricing_context(event)
            self._inject_rating_context(event)

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

                media_plan = self._choose_owned_media_variant(event)
                metadata_for_media = event.get('metadata') or {}
                if not isinstance(metadata_for_media, dict):
                    metadata_for_media = {}
                force_prediction_media = (
                    event.get('source') == 'external_prediction_api'
                    or bool(metadata_for_media.get('prefer_generated_media'))
                )
                experiment_patch = media_plan.get('metadata_patch') or {}
                experiment_meta = experiment_patch.get('media_experiment') or {}
                if (
                    force_prediction_media
                    and media_plan.get('eligible')
                    and experiment_meta.get('policy') != 'disabled'
                ):
                    media_plan['allow_owned_media'] = True
                    media_plan['force_text_only'] = False
                    media_plan['metadata_patch'] = {
                        'media_experiment': {
                            'card_type': 'prediction_card',
                            'variant': 'owned_media_forced',
                            'policy': 'external_prediction',
                            'assigned_at': datetime.now(timezone.utc).isoformat(),
                        }
                    }
                if media_plan.get('metadata_patch'):
                    metadata = event.get('metadata') or {}
                    if not isinstance(metadata, dict):
                        metadata = {}
                    metadata.update(media_plan['metadata_patch'])
                    event['metadata'] = metadata

                # Generate prediction card
                media_attachment = None
                if media_plan.get('force_text_only'):
                    logger.info("📝 Prediction assigned to text-only baseline")
                else:
                    try:
                        media_attachment = self._generate_owned_media(event, tweet_text, pillar, account_bucket)
                    except Exception as e:
                        logger.warning(f"⚠️  Prediction card failed (non-fatal): {e}")

                if self._requires_main_page_media(event, pillar) and (
                    not media_attachment or not media_attachment.get('media_ref')
                ):
                    logger.error("🚫 Prediction blocked: main-feed media is required")
                    with self.db_conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE twitter_bot.events
                            SET status = 'rejected',
                                metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                            WHERE id = %s
                            """,
                            (json.dumps({'media_required_error': 'prediction_missing_media'}), event['id']),
                        )
                        self.db_conn.commit()
                    return False

                media_id = media_attachment.get('media_ref') if media_attachment else None
                media_preview_path = media_attachment.get('preview_path') if media_attachment else self._dashboard_preview_hint(event)
                prediction_status = self._dashboard_review_status('queued')
                prediction_auto_approved = not self.review_only

                # Store directly — auto-approved, no guardrails needed
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO twitter_bot.tweets_v2
                        (event_id, pillar, pillar_name, content, status, account_bucket,
                         fact_checked, tone_validated, generation_model,
                         media_path, media_preview_path, auto_approved, is_thread, thread_tweets)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (
                        event['id'], pillar, 'match_prediction', tweet_text,
                        prediction_status, account_bucket,
                        True, True, 'prediction_template',
                        media_id, media_preview_path, prediction_auto_approved, False, None,
                    ))
                    tweet_id = cur.fetchone()[0]
                    cur.execute(
                        """
                        UPDATE twitter_bot.events
                        SET status = 'scheduled',
                            processed_at = NOW(),
                            metadata = %s
                        WHERE id = %s
                        """,
                        (Json(event.get('metadata') or {}), event['id']),
                    )
                    self.db_conn.commit()

                slot_consumed = True
                if self.review_only:
                    logger.info(f"🧾 Prediction draft {tweet_id} parked in dashboard review")
                else:
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
            tweet_text = normalize_generated_text(tweet_text)
            tweet_text = re.sub(r'@ (\w)', r'@\1', tweet_text)

            if is_invalid_tweet_candidate(tweet_text, pillar=pillar):
                logger.error(f"❌ REJECTED: Tweet leaked meta/review text: {tweet_text[:100]}")
                return False
            if is_thread and thread_tweets:
                thread_tweets = [normalize_generated_text(tt) for tt in thread_tweets]
                for i, tt in enumerate(thread_tweets):
                    if is_invalid_tweet_candidate(tt, pillar=pillar):
                        logger.error(f"❌ REJECTED: Thread tweet {i} leaked meta/review text: {tt[:100]}")
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
            
            main_media_required = self._requires_main_page_media(event, pillar)
            media_plan = self._choose_owned_media_variant(event)
            if media_plan.get('metadata_patch'):
                metadata = event.get('metadata') or {}
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata.update(media_plan['metadata_patch'])
                event['metadata'] = metadata

            # Try to attach media (player images for matches, article images for news)
            media_attachment = None
            media_blocked_by_plan = False
            if media_plan.get('force_text_only'):
                media_blocked_by_plan = (
                    self._media_plan_blocks_fallback(media_plan)
                    and not main_media_required
                )
                logger.info(
                    "📝 Owned media holdout: text-only baseline for %s (%s)",
                    event.get('category'),
                    ((event.get('metadata') or {}).get('media_experiment') or {}).get('card_type', 'n/a'),
                )
            elif event.get('category') == 'engagement_recycle':
                # engagement_recycle has no source_url — Google Images returns random photos.
                # But we CAN generate owned media (team cards, VS cards) from the tweet text.
                try:
                    media_attachment = self._generate_owned_media(event, tweet_text, pillar, account_bucket)
                    if media_attachment and media_attachment.get('media_ref'):
                        logger.info(f"📸 Generated owned media for engagement_recycle: {media_attachment['media_ref']}")
                    else:
                        logger.info("📝 No owned media match for engagement_recycle — posting text-only")
                except Exception as e:
                    logger.warning(f"⚠️  Owned media generation failed for engagement_recycle: {e}")
            else:
                prefer_owned_media = media_plan.get('allow_owned_media', False)
                if not media_plan.get('eligible'):
                    prefer_owned_media = self._prefers_owned_media(event)
                try:
                    if prefer_owned_media:
                        media_attachment = self._generate_owned_media(event, tweet_text, pillar, account_bucket)
                    if not media_attachment or not media_attachment.get('media_ref'):
                        media_ref = self.media_manager.get_media_for_event(
                            event,
                            tweet_text=tweet_text,
                            account_bucket=account_bucket,
                        )
                        media_attachment = self._build_media_attachment(media_ref, self._dashboard_preview_hint(event))
                    if media_attachment and media_attachment.get('media_ref'):
                        logger.info(f"📸 Media attached: {media_attachment['media_ref']}")
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
            if not media_blocked_by_plan and (not media_attachment or not media_attachment.get('media_ref')):
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
                        media_attachment = self._generate_owned_media(event, tweet_text, pillar, account_bucket)
                    except Exception as e:
                        logger.warning(f"⚠️  Card generation failed (non-fatal): {e}")

            # Optional headline-card fallback. Disabled by default because generic
            # cards underperform and look worse than a clean text-only post.
            enable_headline_card_fallback = (
                main_media_required
                or os.getenv('ENABLE_HEADLINE_CARD_FALLBACK', 'false').lower() in ('1', 'true', 'yes')
            )
            if (not media_attachment or not media_attachment.get('media_ref')) and not media_blocked_by_plan and enable_headline_card_fallback:
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
                            media_attachment = self._build_media_attachment(media_id, card_path)
                            logger.info(f"📰 Headline card fallback attached: {media_id}")
                except Exception as e:
                    logger.warning(f"⚠️  Headline card fallback failed (non-fatal): {e}")
            elif not media_attachment or not media_attachment.get('media_ref'):
                logger.info("🖼️ No curated media found — skipping generic headline card fallback")

            if main_media_required and (not media_attachment or not media_attachment.get('media_ref')):
                logger.error("🚫 Main-feed post blocked: media is required")
                with self.db_conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE twitter_bot.events
                        SET status = 'rejected',
                            metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                        WHERE id = %s
                        """,
                        (json.dumps({'media_required_error': 'main_page_missing_media'}), event['id']),
                    )
                    self.db_conn.commit()
                return False

            media_id = media_attachment.get('media_ref') if media_attachment else None
            media_preview_path = media_attachment.get('preview_path') if media_attachment else self._dashboard_preview_hint(event)

            reply_target_id = None
            quote_tweet_id = None
            if pillar == 12 and isinstance(event.get('metadata'), dict):
                target_tweet_id = event['metadata'].get('tweet_id')
                reply_mode = (event['metadata'].get('reply_mode') or '').lower()
                if target_tweet_id and reply_mode == 'reply':
                    reply_target_id = target_tweet_id
                elif target_tweet_id and reply_mode == 'quote':
                    quote_tweet_id = target_tweet_id
                elif target_tweet_id and random.random() < 0.80:
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

            if not auto_approved and pillar == 12 and (reply_target_id or quote_tweet_id) and tone_score >= 7 and fact_passed:
                auto_approved = True
                logger.info(f"🤖 Auto-approved: VIP engagement (fact=✅, tone={tone_score}/10)")

            # Pillar 16 (disagreement replies) always goes through HITL — replies to real
            # people's tweets are high-risk and hard to undo.
            
            # Force HITL for guardrail failures or disagreement replies.
            # VIP engagement can auto-post when guardrails pass; otherwise it stays in HITL.
            if guardrail_failed or pillar == 16:
                auto_approved = False
                if pillar == 16:
                    logger.info("📱 Disagreement reply → forcing HITL review")
            
            scheduled_post_at = None
            status = 'queued' if auto_approved else 'hitl_pending'
            if self.review_only:
                status = self._dashboard_review_status(status)
                auto_approved = False
                logger.info("🧾 Dashboard review-only mode active — parking generated tweet in pending drafts")

            # Peak hour scheduling: stagger non-urgent tweets across 15:00–23:00 UTC
            now_utc = datetime.now(timezone.utc)
            if not self.review_only and not auto_approved and event.get('urgency') != 'breaking':
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
                     media_path, media_preview_path, auto_approved, scheduled_post_at,
                     is_thread, thread_tweets)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    media_preview_path,
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
                    , metadata = %s
                    WHERE id = %s
                """, (Json(event.get('metadata') or {}), event['id']))
                
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
    
    async def run_forever(self, stop_event: Optional[asyncio.Event] = None):
        """Main loop - runs every 5 minutes"""
        stop_event = stop_event or asyncio.Event()
        self.connect_db()
        logger.info("🚀 Tweet Scheduler started")
        self.reconcile_reserved_slots()
        
        while not stop_event.is_set():
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
                            RETURNING COALESCE(account_bucket, 'main')
                        """)
                        hitl_expired_buckets = [row[0] for row in cur.fetchall()]
                        hitl_expired = len(hitl_expired_buckets)
                        
                        self.db_conn.commit()
                        for bucket in hitl_expired_buckets:
                            self.free_slot(bucket)
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
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass
    
    def cleanup(self):
        """Cleanup resources"""
        if self.db_conn:
            self.db_conn.close()


async def main():
    scheduler = TweetScheduler()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_shutdown():
        logger.info("⏹️  Tweet Scheduler shutdown requested")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_args: request_shutdown())
    try:
        await scheduler.run_forever(stop_event)
    except KeyboardInterrupt:
        logger.info("⏹️  Shutting down Tweet Scheduler...")
    finally:
        scheduler.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
