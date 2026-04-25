#!/usr/bin/env python3
"""Internal prediction engine driven by upcoming HLTV fixtures.

This replaces the external signal API path. The process scans upcoming HLTV
matches, prices them with the in-house rating model, and writes
`match_prediction` events directly into the bot pipeline.
"""

import asyncio
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from psycopg2.extras import Json
from scrapling.fetchers import StealthySession

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.hybrid_prediction_pricer import get_hybrid_prediction_pricer
from processing.hltv_analytics_signals import parse_hltv_analytics_context
from processing.team_rating_engine import canonicalize_team_name
from utils.cs2_constants import T1_EVENTS, _has_t1_team
from utils.db_utils import ensure_db_connection

load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv('DATABASE_URL', '')
POLL_INTERVAL = int(os.getenv('PREDICTION_POLL_INTERVAL', '300'))
LOOKAHEAD_HOURS = int(os.getenv('IN_HOUSE_PREDICTION_LOOKAHEAD_HOURS', '36'))
MAX_FIXTURES_PER_CYCLE = int(os.getenv('IN_HOUSE_PREDICTION_MAX_MATCHES', '18'))
MIN_FAVORITE_PROBABILITY_PCT = int(os.getenv('IN_HOUSE_PREDICTION_MIN_PROB_PCT', '53'))
MIN_RATING_GAP = float(os.getenv('IN_HOUSE_PREDICTION_MIN_GAP', '12'))
ANALYTICS_CACHE_HOURS = int(os.getenv('IN_HOUSE_PREDICTION_ANALYTICS_CACHE_HOURS', '6'))
FETCH_TIMEOUT_SECONDS = int(os.getenv('IN_HOUSE_PREDICTION_FETCH_TIMEOUT', '45'))
FIXTURE_TIMEOUT_SECONDS = int(os.getenv('IN_HOUSE_PREDICTION_FIXTURE_TIMEOUT', '75'))
CYCLE_TIMEOUT_SECONDS = int(os.getenv('IN_HOUSE_PREDICTION_CYCLE_TIMEOUT', '180'))
PARSER_TIMEOUT_SECONDS = int(os.getenv('IN_HOUSE_PREDICTION_PARSER_TIMEOUT', '12'))


def _event_is_t1(event_name: str) -> bool:
    event_lower = str(event_name or '').lower()
    return any(marker in event_lower for marker in T1_EVENTS)


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == '':
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _fixture_priority_score(*, is_ranked: bool, is_t1_event: bool, has_t1_team: bool) -> int:
    score = 0
    if is_ranked:
        score += 1
    if is_t1_event:
        score += 2
    if has_t1_team:
        score += 2
    return score


class PredictionWebhook:
    """Legacy process name, now backed by the internal prediction engine."""

    def __init__(self):
        self.db_conn = None
        self.pricer = get_hybrid_prediction_pricer()
        self.session: Optional[StealthySession] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='prediction_hltv')
        self._seen_prediction_keys: set[str] = set()
        self._analytics_cache: Dict[str, Dict[str, Any]] = {}

    def _ensure_db(self):
        self.db_conn = ensure_db_connection(self.db_conn, DATABASE_URL)
        self.pricer.connect_db(self.db_conn)

    async def _ensure_session(self):
        if self.session is not None:
            return
        self.session = StealthySession(solve_cloudflare=True, headless=True)
        await asyncio.get_event_loop().run_in_executor(self._executor, self.session.start)

    async def _reset_session(self, reason: str):
        logger.warning(f"♻️  Resetting HLTV browser session: {reason}")
        old_session = self.session
        old_executor = self._executor
        self.session = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='prediction_hltv')

        if old_session is not None:
            try:
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(old_executor, old_session.close),
                    timeout=5,
                )
            except Exception:
                pass
        old_executor.shutdown(wait=False, cancel_futures=True)

    async def _fetch_page(self, url: str, *, label: str):
        if not url:
            return None

        try:
            await self._ensure_session()
            fetch_future = asyncio.get_event_loop().run_in_executor(
                self._executor,
                partial(
                    self.session.fetch,
                    url,
                    solve_cloudflare=True,
                    network_idle=True,
                ),
            )
            return await asyncio.wait_for(fetch_future, timeout=FETCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(f"⏱️  Timed out fetching {label} after {FETCH_TIMEOUT_SECONDS}s")
            await self._reset_session(f"{label} fetch timeout")
            return None
        except Exception as exc:
            logger.warning(f"⚠️  Failed to fetch {label}: {exc}")
            if 'Target page' in str(exc) or 'browser' in str(exc).lower() or 'context' in str(exc).lower():
                await self._reset_session(f"{label} browser error")
            return None

    async def cleanup(self):
        if self.session is not None:
            try:
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(self._executor, self.session.close),
                    timeout=5,
                )
            except Exception:
                pass
            self.session = None
        self._executor.shutdown(wait=False)

    def _prediction_exists(self, prediction_key: str, match_id: str) -> bool:
        if prediction_key in self._seen_prediction_keys:
            return True

        dedup_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"prediction:{prediction_key}"))
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM twitter_bot.events
                    WHERE id = %s
                       OR (
                            category = 'match_prediction'
                            AND (
                                metadata->>'prediction_key' = %s
                                OR metadata->>'hltv_match_id' = %s
                            )
                       )
                    LIMIT 1
                    """,
                    (dedup_uuid, prediction_key, match_id),
                )
                exists = cur.fetchone() is not None
                if exists:
                    self._seen_prediction_keys.add(prediction_key)
                return exists
        except Exception as exc:
            logger.warning(f"⚠️  Prediction existence check failed for {prediction_key}: {exc}")
            return False

    @staticmethod
    def _extract_match_link(node, match_id: str) -> str:
        for anchor in node.css('a'):
            href = anchor.attrib.get('href') or ''
            if f'/matches/{match_id}/' in href:
                return f'https://www.hltv.org{href}'
        return f'https://www.hltv.org/matches/{match_id}'

    @staticmethod
    def _analytics_url_for_fixture(fixture: Dict[str, Any]) -> str:
        source_url = str(fixture.get('source_url') or '').strip()
        if '/betting/analytics/' in source_url:
            return source_url
        if '/matches/' in source_url:
            return source_url.replace('/matches/', '/betting/analytics/', 1)
        match_id = str(fixture.get('match_id') or '').strip()
        return f'https://www.hltv.org/betting/analytics/{match_id}' if match_id else ''

    async def _fetch_analytics_context(
        self,
        fixture: Dict[str, Any],
        pick_team: str,
    ) -> Optional[Dict[str, Any]]:
        match_id = str(fixture.get('match_id') or '').strip()
        now_utc = datetime.now(timezone.utc)
        cached = self._analytics_cache.get(match_id)
        if cached:
            cached_at = cached.get('fetched_at')
            if isinstance(cached_at, datetime) and now_utc - cached_at < timedelta(hours=ANALYTICS_CACHE_HOURS):
                return cached.get('context')

        analytics_url = self._analytics_url_for_fixture(fixture)
        if not analytics_url:
            return None

        page = await self._fetch_page(
            analytics_url,
            label=f"HLTV analytics {fixture.get('match_id')}",
        )
        if not page:
            return None

        try:
            context = await asyncio.wait_for(
                asyncio.to_thread(
                    parse_hltv_analytics_context,
                    page.html_content,
                    team_a=fixture['team_a'],
                    team_b=fixture['team_b'],
                    pick_team=pick_team,
                ),
                timeout=PARSER_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"⏱️  Timed out parsing HLTV analytics for {fixture.get('match_id')} "
                f"after {PARSER_TIMEOUT_SECONDS}s"
            )
            return None
        except Exception as exc:
            logger.warning(f"⚠️  Failed to parse HLTV analytics for {fixture.get('match_id')}: {exc}")
            return None
        if not context:
            return None

        context['source_url'] = analytics_url
        self._analytics_cache[match_id] = {
            'fetched_at': now_utc,
            'context': context,
        }
        return context

    async def _fetch_upcoming_fixtures(self) -> List[Dict[str, Any]]:
        page = await self._fetch_page('https://www.hltv.org/matches', label='HLTV matches')
        if not page:
            return []

        now_utc = datetime.now(timezone.utc)
        latest_allowed = now_utc + timedelta(hours=LOOKAHEAD_HOURS)
        fixtures: Dict[str, Dict[str, Any]] = {}

        for node in page.css('.matches-list .match-wrapper'):
            match_id = str(node.attrib.get('data-match-id') or '').strip()
            if not match_id:
                continue
            if str(node.attrib.get('live') or '').lower() == 'true':
                continue

            time_nodes = node.css('.match-time')
            unix_ms = _safe_int(time_nodes[0].attrib.get('data-unix')) if time_nodes else None
            if unix_ms is None:
                continue
            scheduled_at = datetime.fromtimestamp(unix_ms / 1000.0, tz=timezone.utc)
            if scheduled_at < now_utc - timedelta(minutes=15) or scheduled_at > latest_allowed:
                continue

            team_names = [n.text.strip() for n in node.css('.match-teamname') if n.text and n.text.strip()]
            if len(team_names) < 2:
                continue
            team_a = team_names[0]
            team_b = team_names[1]
            if not team_a or not team_b:
                continue

            event_name = ''
            event_nodes = node.css('.match-event')
            if event_nodes:
                event_name = (
                    event_nodes[0].attrib.get('data-event-headline')
                    or event_nodes[0].text.strip()
                )

            event_type = str(node.attrib.get('data-eventtype') or '').lower()
            is_ranked = event_type == 'ranked'
            is_t1_event = _event_is_t1(event_name)
            has_t1_team = _has_t1_team(f"{team_a} {team_b}".lower())
            if not (is_ranked or is_t1_event or has_t1_team):
                continue

            priority_score = _fixture_priority_score(
                is_ranked=is_ranked,
                is_t1_event=is_t1_event,
                has_t1_team=has_t1_team,
            )

            meta_nodes = node.css('.match-meta')
            match_format = meta_nodes[0].text.strip().upper() if meta_nodes else ''
            existing = fixtures.get(match_id, {})
            fixtures[match_id] = {
                'match_id': match_id,
                'team_a': team_a or existing.get('team_a', ''),
                'team_b': team_b or existing.get('team_b', ''),
                'event_name': event_name or existing.get('event_name', ''),
                'format': match_format or existing.get('format', ''),
                'scheduled_at': scheduled_at,
                'source_url': self._extract_match_link(node, match_id) or existing.get('source_url', ''),
                'priority_score': max(priority_score, int(existing.get('priority_score', 0) or 0)),
            }

        ordered = sorted(
            fixtures.values(),
            key=lambda item: (
                -int(item.get('priority_score', 0) or 0),
                item['scheduled_at'],
            ),
        )
        return ordered[:MAX_FIXTURES_PER_CYCLE]

    @staticmethod
    def _pick_confidence(favorite_prob_pct: int, confidence_label: str, rating_gap: float) -> str:
        if favorite_prob_pct >= 60 and confidence_label != 'low':
            return 'strong'
        if favorite_prob_pct >= 57 and rating_gap >= 20:
            return 'strong'
        return 'standard'

    async def _build_prediction_from_fixture(self, fixture: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self._ensure_db()
        rating_context = self.pricer.rating_engine.get_matchup_context(
            team_a=fixture['team_a'],
            team_b=fixture['team_b'],
            event_name=fixture['event_name'],
        )
        if not rating_context:
            return None

        favorite_prob_pct = int(rating_context.get('favorite_win_probability_pct', 0) or 0)
        rating_gap = float(rating_context.get('rating_gap', 0) or 0)
        confidence_label = str(rating_context.get('confidence_label') or 'low')
        team_a_matches = int(rating_context.get('team_a_matches', 0) or 0)
        team_b_matches = int(rating_context.get('team_b_matches', 0) or 0)
        combined_matches = team_a_matches + team_b_matches
        if favorite_prob_pct < MIN_FAVORITE_PROBABILITY_PCT:
            return None
        if confidence_label == 'low' and combined_matches < 2:
            return None
        if confidence_label == 'low' and rating_gap < MIN_RATING_GAP:
            return None

        favorite_key = rating_context.get('favorite_key')
        team_a_key = canonicalize_team_name(fixture['team_a'])
        pick = fixture['team_a'] if favorite_key == team_a_key else fixture['team_b']
        opponent = fixture['team_b'] if pick == fixture['team_a'] else fixture['team_a']
        analytics_context = await self._fetch_analytics_context(fixture, pick)

        pricing_context = self.pricer.price_prediction(
            team_a=fixture['team_a'],
            team_b=fixture['team_b'],
            pick_team=pick,
            market_type='match_winner',
            pick_odds=None,
            provider_win_probability=None,
            analytics_context=analytics_context,
            event_name=fixture['event_name'],
        )
        if not pricing_context:
            return None

        hybrid_prob_pct = int(pricing_context.get('hybrid_win_probability_pct', 0) or 0)
        if hybrid_prob_pct < MIN_FAVORITE_PROBABILITY_PCT:
            return None

        rating_context = pricing_context.pop('rating_context', rating_context)
        prediction_key = f"hltv:{fixture['match_id']}"
        confidence = self._pick_confidence(hybrid_prob_pct, confidence_label, rating_gap)
        scheduled_at = fixture['scheduled_at']
        scheduled_label = scheduled_at.strftime('%H:%M UTC')
        pick_line = pricing_context.get('pick_line') or pricing_context.get('tweet_line')

        metadata = {
            'team1': pick,
            'team2': opponent,
            'team_a': fixture['team_a'],
            'team_b': fixture['team_b'],
            'pick': pick,
            'pick_odds': None,
            'confidence': confidence,
            'edge_pct': pricing_context.get('display_edge_pct', 0.0),
            'win_probability': pricing_context.get('hybrid_win_probability'),
            'provider_edge_pct': None,
            'provider_win_probability': None,
            'event': fixture['event_name'],
            'format': fixture['format'],
            'market_type': 'match_winner',
            'factors': [],
            'match_id': fixture['match_id'],
            'hltv_match_id': fixture['match_id'],
            'bet_id': prediction_key,
            'prediction_key': prediction_key,
            'scheduled_at': scheduled_at.isoformat(),
            'fixture_priority_score': fixture.get('priority_score', 0),
            'source_model': pricing_context.get('model_version'),
            'pricing_context': pricing_context,
            'rating_context': rating_context,
            'analytics_context': analytics_context,
            'internal_win_probability': pricing_context.get('hybrid_win_probability'),
            'internal_edge_pct': pricing_context.get('signed_edge_pct'),
            'internal_fair_odds': pricing_context.get('fair_odds'),
        }

        content_parts = [
            f"{fixture['team_a']} vs {fixture['team_b']}",
            f"Pick: {pick}",
            f"Win probability: {pricing_context.get('hybrid_win_probability', 0):.1%}",
            f"Confidence: {confidence}",
            f"Format: {fixture['format']}" if fixture['format'] else '',
            f"Event: {fixture['event_name']}" if fixture['event_name'] else '',
            f"Start: {scheduled_label}",
        ]
        if pick_line:
            content_parts.append(pick_line)

        return {
            'prediction_key': prediction_key,
            'headline': f"Prediction: {pick} over {opponent} ({confidence})",
            'content': '\n'.join(part for part in content_parts if part),
            'metadata': metadata,
            'source_url': fixture['source_url'],
            'pick': pick,
            'opponent': opponent,
            'confidence': confidence,
        }

    def _insert_prediction_event(self, prediction: Dict[str, Any]) -> bool:
        prediction_key = prediction['prediction_key']
        match_id = str(prediction['metadata'].get('hltv_match_id') or '')
        if self._prediction_exists(prediction_key, match_id):
            logger.info(f"⏭️  Prediction {prediction_key} already exists, skipping")
            return False

        dedup_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"prediction:{prediction_key}"))
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO twitter_bot.events
                    (id, siftly_event_id, headline, content, category,
                     urgency, status, metadata, source, source_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        dedup_uuid,
                        None,
                        prediction['headline'],
                        prediction['content'],
                        'match_prediction',
                        'normal',
                        'pending',
                        Json(prediction['metadata']),
                        'prediction_model',
                        prediction['source_url'],
                    ),
                )
                self.db_conn.commit()
            self._seen_prediction_keys.add(prediction_key)
            logger.info(
                f"✅ In-house prediction created: {prediction['pick']} over "
                f"{prediction['opponent']} ({prediction['confidence']})"
            )
            return True
        except Exception as exc:
            logger.error(f"❌ Failed to insert in-house prediction {prediction_key}: {exc}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass
            return False

    async def run_cycle(self):
        fixtures = await self._fetch_upcoming_fixtures()
        if not fixtures:
            logger.info("📭 No tweetworthy upcoming HLTV fixtures found")
            return

        inserted = 0
        for fixture in fixtures:
            try:
                prediction = await asyncio.wait_for(
                    self._build_prediction_from_fixture(fixture),
                    timeout=FIXTURE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"⏱️  Timed out building prediction for HLTV {fixture.get('match_id')} "
                    f"after {FIXTURE_TIMEOUT_SECONDS}s"
                )
                await self._reset_session(f"fixture {fixture.get('match_id')} timeout")
                continue
            if not prediction:
                continue
            if self._insert_prediction_event(prediction):
                inserted += 1

        logger.info(f"🔎 Scanned {len(fixtures)} upcoming fixtures, created {inserted} predictions")


async def main():
    engine = PredictionWebhook()
    logger.info("🚀 Internal prediction engine started")
    logger.info(
        f"   Poll interval: {POLL_INTERVAL}s | Lookahead: {LOOKAHEAD_HOURS}h | "
        f"Min fair prob: {MIN_FAVORITE_PROBABILITY_PCT}%"
    )

    try:
        while True:
            try:
                await asyncio.wait_for(engine.run_cycle(), timeout=CYCLE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error(f"⏱️  Prediction cycle timed out after {CYCLE_TIMEOUT_SECONDS}s")
                await engine._reset_session("cycle timeout")
            except Exception as exc:
                logger.error(f"❌ Prediction cycle error: {exc}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)
    finally:
        await engine.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
