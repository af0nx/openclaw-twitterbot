#!/usr/bin/env python3
"""Prediction event ingester.

When EXTERNAL_PREDICTION_API_URL and EXTERNAL_PREDICTION_API_TOKEN are set this
polls the private external prediction API. If those env vars are absent, it
falls back to the older in-house HLTV scanner.
"""

import asyncio
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
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

EXTERNAL_PREDICTION_API_URL = os.getenv('EXTERNAL_PREDICTION_API_URL', '').rstrip('/')
EXTERNAL_PREDICTION_API_TOKEN = os.getenv('EXTERNAL_PREDICTION_API_TOKEN', '')
EXTERNAL_PREDICTION_API_TIMEOUT_MS = int(os.getenv('EXTERNAL_PREDICTION_API_TIMEOUT_MS', '5000'))
EXTERNAL_PREDICTION_API_LIMIT = int(os.getenv('EXTERNAL_PREDICTION_API_LIMIT', '8'))
EXTERNAL_PREDICTION_API_ENABLED = bool(EXTERNAL_PREDICTION_API_URL and EXTERNAL_PREDICTION_API_TOKEN)
IN_HOUSE_PREDICTION_FALLBACK = os.getenv('IN_HOUSE_PREDICTION_FALLBACK', 'false').lower() in (
    '1', 'true', 'yes', 'on'
)


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


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == '':
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _ascii_line(value: Any, max_len: int = 140) -> str:
    line = str(value or '').encode('ascii', 'ignore').decode('ascii')
    line = ' '.join(line.replace('\n', ' ').split())
    return line[:max_len].strip()


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

    @staticmethod
    def _external_factor_lines(reasoning_summary: str, pick: str) -> List[str]:
        summary = _ascii_line(reasoning_summary, 240)
        if not summary:
            return []

        lower = summary.lower()
        factors: List[str] = []
        if 'ranking' in lower:
            factors.append(f"Ranking points toward {pick}")
        if 'form' in lower:
            factors.append("Recent form matters here")
        if 'value' in lower or 'edge' in lower:
            factors.append("Price leaves some value")
        if not factors:
            factors.append(summary)
        return factors[:3]

    def _external_to_prediction(
        self,
        item: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        external_id = str(item.get('externalId') or '').strip()
        match_id = str(item.get('matchId') or external_id).strip()
        team_a = _ascii_line(item.get('teamA'), 80)
        team_b = _ascii_line(item.get('teamB'), 80)
        pick = _ascii_line(item.get('pick'), 80)
        if not match_id or not team_a or not team_b or not pick:
            return None
        if pick not in {team_a, team_b}:
            return None

        status = str(item.get('status') or '').strip().lower()
        if status and status not in {'active', 'pending', 'ready'}:
            return None

        now_utc = datetime.now(timezone.utc)
        expires_at = _parse_iso_utc(item.get('expiresAt'))
        if expires_at and expires_at < now_utc - timedelta(minutes=5):
            return None

        start_time = _parse_iso_utc(item.get('startTime') or item.get('scheduledAt'))
        opponent = team_b if pick == team_a else team_a
        prediction_key = f"external:{external_id or match_id}"

        win_pct = _safe_float(item.get('winProbabilityPct'))
        win_probability = (win_pct / 100.0) if win_pct and win_pct > 1 else (win_pct or 0.0)
        fair_odds = _safe_float(item.get('fairOdds'))
        market_odds = _safe_float(item.get('marketOdds'))
        edge_pct = _safe_float(item.get('edgePct')) or 0.0
        confidence = _ascii_line(item.get('confidence') or 'standard', 32).lower() or 'standard'
        verdict = _ascii_line(item.get('verdict'), 40)
        reasoning = _ascii_line(item.get('reasoningSummary'), 240)
        factors = self._external_factor_lines(reasoning, pick)

        event_name = _ascii_line(
            item.get('eventName') or item.get('tournament') or item.get('event') or 'CS2 match',
            80,
        )
        config = payload.get('config') if isinstance(payload.get('config'), dict) else {}
        health = payload.get('health') if isinstance(payload.get('health'), dict) else {}
        model_version = _ascii_line(config.get('modelVersion') or payload.get('source'), 80)
        source_updated_at = _ascii_line(item.get('sourceUpdatedAt') or health.get('lastUpdatedAt'), 40)

        pricing_context = {
            'model_version': model_version or 'external-vps',
            'display_edge_pct': edge_pct,
            'signed_edge_pct': edge_pct,
            'fair_odds': fair_odds,
            'market_odds': market_odds,
            'verdict': verdict,
            'pick_line': f"{pick} gets {win_pct:.0f}% from the model" if win_pct else '',
            'provider_line': factors[0] if factors else '',
        }

        metadata = {
            'team1': pick,
            'team2': opponent,
            'team_a': team_a,
            'team_b': team_b,
            'pick': pick,
            'pick_odds': market_odds if market_odds and market_odds > 1.01 else None,
            'fair_odds': fair_odds,
            'market_odds': market_odds,
            'confidence': confidence,
            'edge_pct': edge_pct,
            'win_probability': win_probability,
            'win_probability_pct': win_pct,
            'provider_edge_pct': edge_pct,
            'provider_win_probability': win_probability,
            'event': event_name,
            'format': _ascii_line(item.get('format') or item.get('matchFormat'), 24),
            'market_type': 'match_winner',
            'factors': factors,
            'match_id': match_id,
            'hltv_match_id': match_id,
            'bet_id': prediction_key,
            'prediction_key': prediction_key,
            'external_prediction_id': external_id,
            'external_source': _ascii_line(payload.get('source') or 'external-vps', 80),
            'source_model': model_version or 'external-vps',
            'source_updated_at': source_updated_at,
            'scheduled_at': start_time.isoformat() if start_time else None,
            'expires_at': expires_at.isoformat() if expires_at else None,
            'reasoning_summary': reasoning,
            'verdict': verdict,
            'pricing_context': pricing_context,
            'prefer_generated_media': True,
        }

        scheduled_label = start_time.strftime('%H:%M UTC') if start_time else ''
        content_parts = [
            f"{team_a} vs {team_b}",
            f"Pick: {pick}",
            f"Win probability: {win_pct:.0f}%" if win_pct else '',
            f"Market odds: {market_odds:.2f}" if market_odds else '',
            f"Edge: {edge_pct:.1f}%" if edge_pct else '',
            f"Confidence: {confidence}",
            f"Event: {event_name}" if event_name else '',
            f"Start: {scheduled_label}" if scheduled_label else '',
            f"Reason: {reasoning}" if reasoning else '',
        ]

        source_ref = quote(external_id or match_id, safe='')

        return {
            'prediction_key': prediction_key,
            'headline': f"Prediction: {pick} over {opponent} ({confidence})",
            'content': '\n'.join(part for part in content_parts if part),
            'metadata': metadata,
            'source': 'external_prediction_api',
            'source_url': f"{EXTERNAL_PREDICTION_API_URL}/v1/predictions/dashboard?externalId={source_ref}",
            'pick': pick,
            'opponent': opponent,
            'confidence': confidence,
        }

    async def _fetch_external_predictions(self) -> List[Dict[str, Any]]:
        url = f"{EXTERNAL_PREDICTION_API_URL}/v1/predictions/dashboard"
        headers = {
            'Authorization': f"Bearer {EXTERNAL_PREDICTION_API_TOKEN}",
            'Accept': 'application/json',
            'User-Agent': 'openclaw-twitter-bot/1.0',
        }
        params = {
            'limit': EXTERNAL_PREDICTION_API_LIMIT,
            'lookaheadHours': LOOKAHEAD_HOURS,
        }
        timeout = max(1.0, EXTERNAL_PREDICTION_API_TIMEOUT_MS / 1000.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()

        recent = payload.get('recent') if isinstance(payload, dict) else None
        if not isinstance(recent, list):
            raise ValueError('external prediction API returned no recent list')

        predictions = []
        for item in recent:
            if not isinstance(item, dict):
                continue
            prediction = self._external_to_prediction(item, payload)
            if prediction:
                predictions.append(prediction)

        health = payload.get('health') if isinstance(payload.get('health'), dict) else {}
        logger.info(
            f"🌐 External prediction API health={health.get('status', 'unknown')} "
            f"recent={len(recent)} mapped={len(predictions)}"
        )
        return predictions

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
                        prediction.get('source') or 'prediction_model',
                        prediction['source_url'],
                    ),
                )
                self.db_conn.commit()
            self._seen_prediction_keys.add(prediction_key)
            logger.info(
                f"✅ Prediction created: {prediction['pick']} over "
                f"{prediction['opponent']} ({prediction['confidence']})"
            )
            return True
        except Exception as exc:
            logger.error(f"❌ Failed to insert prediction {prediction_key}: {exc}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass
            return False

    async def run_cycle(self):
        if EXTERNAL_PREDICTION_API_ENABLED:
            try:
                predictions = await self._fetch_external_predictions()
            except Exception as exc:
                logger.error(f"❌ External prediction API cycle failed: {exc}", exc_info=True)
                if not IN_HOUSE_PREDICTION_FALLBACK:
                    return
                logger.warning("↩️  Falling back to in-house HLTV prediction scanner")
            else:
                inserted = 0
                for prediction in predictions:
                    if self._insert_prediction_event(prediction):
                        inserted += 1
                logger.info(
                    f"🔎 Polled external predictions, created {inserted} new events"
                )
                return

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
    if EXTERNAL_PREDICTION_API_ENABLED:
        logger.info("🚀 External prediction API ingester started")
        logger.info(
            f"   Poll interval: {POLL_INTERVAL}s | Lookahead: {LOOKAHEAD_HOURS}h | "
            f"Limit: {EXTERNAL_PREDICTION_API_LIMIT} | Fallback: {IN_HOUSE_PREDICTION_FALLBACK}"
        )
    else:
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
