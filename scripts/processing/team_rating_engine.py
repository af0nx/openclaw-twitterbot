#!/usr/bin/env python3
"""Deterministic in-house team ratings built from live match results.

This module is a lightweight pre-match team prior. It is not a player-level
Glicko-2 map-side system, not a live round model, and not a market-closing-line
engine. Output fair lines are confidence-shrunk because the current source data
is sparse `match_result` history rather than a full historical warehouse.
"""

import logging
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.cs2_constants import T1_EVENTS


logger = logging.getLogger(__name__)

BASE_RATING = 1500.0
BASE_K = 28.0
MAX_K = 44.0
CACHE_TTL = timedelta(minutes=15)

TEAM_ALIASES = {
    'natus vincere': 'navi',
    'na vi': 'navi',
    'navi': 'navi',
    'team vitality': 'vitality',
    'vitality': 'vitality',
    'faze clan': 'faze',
    'faze': 'faze',
    'g2 esports': 'g2',
    'g2': 'g2',
    'team liquid': 'liquid',
    'liquid': 'liquid',
    'mousesports': 'mouz',
    'mouz': 'mouz',
    'team spirit': 'spirit',
    'spirit': 'spirit',
    'heroic': 'heroic',
    'fnatic': 'fnatic',
    'furia esports': 'furia',
    'furia': 'furia',
    'astralis': 'astralis',
    'complexity gaming': 'complexity',
    'complexity': 'complexity',
    'col': 'complexity',
    'eternal fire': 'eternal fire',
    'ef': 'eternal fire',
    'virtus pro': 'virtus.pro',
    'virtuspro': 'virtus.pro',
    'virtus.pro': 'virtus.pro',
    'vp': 'virtus.pro',
    'cloud9': 'cloud9',
    'c9': 'cloud9',
    'big clan': 'big',
    'big': 'big',
    'imperial esports': 'imperial',
    'imperial': 'imperial',
    'pain gaming': 'pain',
    'pain': 'pain',
    'saw': 'saw',
    'apeks': 'apeks',
    'gamerlegion': 'gamerlegion',
    'sinners': 'sinners',
    '3dmax': '3dmax',
    'ence': 'ence',
    'mibr': 'mibr',
    'the mongolz': 'mongolz',
    'mongolz': 'mongolz',
    'lynn vision gaming': 'lynn vision',
    'lynn vision': 'lynn vision',
}

DISPLAY_NAMES = {
    'navi': 'NaVi',
    'vitality': 'Vitality',
    'faze': 'FaZe',
    'g2': 'G2',
    'liquid': 'Liquid',
    'mouz': 'MOUZ',
    'spirit': 'Spirit',
    'heroic': 'Heroic',
    'fnatic': 'Fnatic',
    'furia': 'FURIA',
    'astralis': 'Astralis',
    'complexity': 'Complexity',
    'eternal fire': 'Eternal Fire',
    'virtus.pro': 'Virtus.pro',
    'cloud9': 'Cloud9',
    'big': 'BIG',
    'imperial': 'Imperial',
    'pain': 'paiN',
    'saw': 'SAW',
    'apeks': 'Apeks',
    'gamerlegion': 'GamerLegion',
    'sinners': 'Sinners',
    '3dmax': '3DMAX',
    'ence': 'ENCE',
    'mibr': 'MIBR',
    'mongolz': 'The MongolZ',
    'lynn vision': 'Lynn Vision',
}

RESULT_PATTERNS = (
    re.compile(
        r'^\s*(?P<winner>.+?)\s+(?:defeats?|beat|beats|eliminate(?:s|d)?|edges?|stun(?:s|ned)?|take(?:s|n)? down)\s+(?P<loser>.+?)\s+(?P<score>\d+\s*-\s*\d+)(?:\s+in\s+(?P<event>.+))?$',
        re.IGNORECASE,
    ),
    re.compile(
        r'^\s*(?P<winner>.+?)\s+(?:overcome(?:s|d)?|upset(?:s)?|dispatch(?:es|ed)?)\s+(?P<loser>.+?)\s+(?P<score>\d+\s*-\s*\d+)(?:\s+in\s+(?P<event>.+))?$',
        re.IGNORECASE,
    ),
)


def _normalize_token(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(text or '').lower()).strip()


def canonicalize_team_name(name: str) -> Optional[str]:
    cleaned = _normalize_token(name)
    if not cleaned:
        return None
    if cleaned in TEAM_ALIASES:
        return TEAM_ALIASES[cleaned]
    if cleaned.startswith('team '):
        trimmed = cleaned[5:].strip()
        if trimmed:
            return TEAM_ALIASES.get(trimmed, trimmed)
    if cleaned.startswith('the '):
        trimmed = cleaned[4:].strip()
        if trimmed in TEAM_ALIASES:
            return TEAM_ALIASES[trimmed]
    return cleaned


def display_team_name(raw_name: str, team_key: Optional[str] = None) -> str:
    key = team_key or canonicalize_team_name(raw_name)
    if key in DISPLAY_NAMES:
        return DISPLAY_NAMES[key]
    raw = str(raw_name or '').strip()
    if raw:
        return raw
    if not key:
        return 'Unknown'
    return ' '.join(part.upper() if len(part) <= 3 and any(c.isdigit() for c in part) else part.capitalize() for part in key.split())


def _expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def _confidence_label(confidence: float) -> str:
    if confidence >= 0.75:
        return 'high'
    if confidence >= 0.45:
        return 'medium'
    return 'low'


class TeamRatingEngine:
    """Lightweight Elo-style pre-match team ratings derived from `match_result` events."""

    def __init__(self):
        self.db_conn = None
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_created_at: Optional[datetime] = None

    def connect_db(self, conn=None):
        self.db_conn = conn

    @staticmethod
    def _coerce_datetime(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        return None

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            if value is None or value == '':
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def parse_match_row(cls, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
        headline = str(row.get('headline') or '').strip()
        created_at = cls._coerce_datetime(row.get('created_at'))

        team1 = None
        team2 = None
        teams = metadata.get('teams')
        if isinstance(teams, list) and len(teams) >= 2:
            team1, team2 = teams[0], teams[1]
        else:
            team1 = metadata.get('team1')
            team2 = metadata.get('team2')

        parsed_event = None
        if headline:
            for pattern in RESULT_PATTERNS:
                match = pattern.match(headline)
                if match:
                    parsed_event = match.groupdict()
                    break

        if not team1 and parsed_event:
            team1 = parsed_event.get('winner')
        if not team2 and parsed_event:
            team2 = parsed_event.get('loser')

        team1_key = canonicalize_team_name(team1)
        team2_key = canonicalize_team_name(team2)
        if not team1_key or not team2_key or team1_key == team2_key:
            return None

        score1 = cls._coerce_int(metadata.get('score1'))
        score2 = cls._coerce_int(metadata.get('score2'))
        if score1 is None or score2 is None:
            score_value = metadata.get('score') or (parsed_event or {}).get('score') or ''
            score_match = re.search(r'(\d+)\s*-\s*(\d+)', str(score_value))
            if score_match:
                score1 = int(score_match.group(1))
                score2 = int(score_match.group(2))

        winner_key = canonicalize_team_name(metadata.get('winner'))
        if not winner_key and parsed_event:
            winner_key = canonicalize_team_name(parsed_event.get('winner'))
        if not winner_key and score1 is not None and score2 is not None and score1 != score2:
            winner_key = team1_key if score1 > score2 else team2_key
        if winner_key not in {team1_key, team2_key}:
            return None

        event_name = str(metadata.get('event') or metadata.get('event_name') or (parsed_event or {}).get('event') or '').strip()

        return {
            'team1_key': team1_key,
            'team2_key': team2_key,
            'team1_name': display_team_name(team1, team1_key),
            'team2_name': display_team_name(team2, team2_key),
            'winner_key': winner_key,
            'score1': score1,
            'score2': score2,
            'event_name': event_name,
            'played_at': created_at,
        }

    @staticmethod
    def _fresh_snapshot(team_key: str, team_name: str) -> Dict[str, Any]:
        return {
            'team_key': team_key,
            'team_name': team_name,
            'rating': BASE_RATING,
            'matches_played': 0,
            'wins': 0,
            'losses': 0,
            'recent_results': deque(maxlen=5),
            'recent_delta': 0.0,
            'recent_opponents': deque(maxlen=3),
            'last_match_at': None,
        }

    @staticmethod
    def _is_t1_event(event_name: str) -> bool:
        event_lower = str(event_name or '').lower()
        return any(marker in event_lower for marker in T1_EVENTS)

    def _match_k_factor(
        self,
        team_a_matches: int,
        team_b_matches: int,
        event_name: str,
        score1: Optional[int],
        score2: Optional[int],
        played_at: Optional[datetime],
    ) -> float:
        min_matches = min(team_a_matches, team_b_matches)
        if min_matches < 3:
            k_factor = MAX_K
        elif min_matches < 8:
            k_factor = 36.0
        else:
            k_factor = BASE_K

        if self._is_t1_event(event_name):
            k_factor *= 1.10

        if score1 is not None and score2 is not None:
            margin = abs(score1 - score2)
            k_factor *= 1.0 + min(0.18, margin * 0.08)

        if played_at:
            age_days = max(0.0, (datetime.now(timezone.utc) - played_at).total_seconds() / 86400.0)
            if age_days <= 30:
                k_factor *= 1.08
            elif age_days >= 180:
                k_factor *= 0.92

        return max(18.0, min(k_factor, 52.0))

    def build_ratings_from_rows(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        ratings: Dict[str, Dict[str, Any]] = {}
        parsed_matches = 0

        for row in rows:
            match = self.parse_match_row(row)
            if not match:
                continue

            team1_key = match['team1_key']
            team2_key = match['team2_key']
            team1 = ratings.setdefault(team1_key, self._fresh_snapshot(team1_key, match['team1_name']))
            team2 = ratings.setdefault(team2_key, self._fresh_snapshot(team2_key, match['team2_name']))
            team1['team_name'] = match['team1_name'] or team1['team_name']
            team2['team_name'] = match['team2_name'] or team2['team_name']

            expected_team1 = _expected_score(team1['rating'], team2['rating'])
            actual_team1 = 1.0 if match['winner_key'] == team1_key else 0.0
            k_factor = self._match_k_factor(
                team1['matches_played'],
                team2['matches_played'],
                match['event_name'],
                match['score1'],
                match['score2'],
                match['played_at'],
            )
            delta = k_factor * (actual_team1 - expected_team1)

            team1['rating'] += delta
            team2['rating'] -= delta
            team1['matches_played'] += 1
            team2['matches_played'] += 1
            team1['wins'] += int(actual_team1 == 1.0)
            team2['wins'] += int(actual_team1 == 0.0)
            team1['losses'] += int(actual_team1 == 0.0)
            team2['losses'] += int(actual_team1 == 1.0)
            team1['recent_results'].append('W' if actual_team1 == 1.0 else 'L')
            team2['recent_results'].append('W' if actual_team1 == 0.0 else 'L')
            team1['recent_opponents'].append(team2['team_name'])
            team2['recent_opponents'].append(team1['team_name'])
            team1['recent_delta'] += delta
            team2['recent_delta'] -= delta

            played_at = match['played_at']
            if played_at and (not team1['last_match_at'] or played_at > team1['last_match_at']):
                team1['last_match_at'] = played_at
            if played_at and (not team2['last_match_at'] or played_at > team2['last_match_at']):
                team2['last_match_at'] = played_at

            parsed_matches += 1

        snapshots: Dict[str, Dict[str, Any]] = {}
        now_utc = datetime.now(timezone.utc)
        for team_key, snapshot in ratings.items():
            last_match_at = snapshot['last_match_at']
            age_days = max(0.0, (now_utc - last_match_at).total_seconds() / 86400.0) if last_match_at else 365.0
            sample_component = min(1.0, snapshot['matches_played'] / 8.0)
            freshness_component = max(0.2, 1.0 - (age_days / 180.0))
            confidence = round((sample_component * 0.7) + (freshness_component * 0.3), 2)
            recent_delta = round(snapshot['recent_delta'], 1)
            if recent_delta >= 30:
                trend_label = 'heating up'
            elif recent_delta <= -30:
                trend_label = 'cooling off'
            else:
                trend_label = 'stable'

            snapshots[team_key] = {
                'team_key': team_key,
                'team_name': snapshot['team_name'],
                'rating': round(snapshot['rating'], 1),
                'matches_played': snapshot['matches_played'],
                'wins': snapshot['wins'],
                'losses': snapshot['losses'],
                'recent_form': ''.join(snapshot['recent_results']) or '-',
                'recent_delta': recent_delta,
                'confidence': confidence,
                'confidence_label': _confidence_label(confidence),
                'last_match_at': last_match_at.isoformat() if last_match_at else None,
                'metadata': {
                    'recent_results': list(snapshot['recent_results']),
                    'recent_opponents': list(snapshot['recent_opponents']),
                    'trend_label': trend_label,
                    'rating_version': 'elo_v1',
                },
            }

        return {
            'teams': snapshots,
            'parsed_matches': parsed_matches,
            'rating_version': 'elo_v1',
            'generated_at': now_utc.isoformat(),
        }

    def _query_match_rows(self) -> List[Dict[str, Any]]:
        if not self.db_conn:
            return []
        with self.db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT headline, metadata, created_at
                FROM twitter_bot.events
                WHERE category = 'match_result'
                  AND COALESCE(status, 'pending') <> 'rejected'
                  AND created_at > NOW() - INTERVAL '365 days'
                ORDER BY created_at ASC, id ASC
                """
            )
            return [
                {
                    'headline': row[0],
                    'metadata': row[1],
                    'created_at': row[2],
                }
                for row in cur.fetchall()
            ]

    def _persist_ratings(self, ratings_state: Dict[str, Any]):
        if not self.db_conn:
            return
        with self.db_conn.cursor() as cur:
            for snapshot in ratings_state.get('teams', {}).values():
                cur.execute(
                    """
                    INSERT INTO twitter_bot.team_ratings (
                        team_key, team_name, rating, matches_played, wins, losses,
                        recent_form, recent_delta, confidence, rating_version,
                        last_match_at, last_rebuilt_at, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                    ON CONFLICT (team_key) DO UPDATE SET
                        team_name = EXCLUDED.team_name,
                        rating = EXCLUDED.rating,
                        matches_played = EXCLUDED.matches_played,
                        wins = EXCLUDED.wins,
                        losses = EXCLUDED.losses,
                        recent_form = EXCLUDED.recent_form,
                        recent_delta = EXCLUDED.recent_delta,
                        confidence = EXCLUDED.confidence,
                        rating_version = EXCLUDED.rating_version,
                        last_match_at = EXCLUDED.last_match_at,
                        last_rebuilt_at = NOW(),
                        metadata = EXCLUDED.metadata
                    """,
                    (
                        snapshot['team_key'],
                        snapshot['team_name'],
                        snapshot['rating'],
                        snapshot['matches_played'],
                        snapshot['wins'],
                        snapshot['losses'],
                        snapshot['recent_form'],
                        snapshot['recent_delta'],
                        snapshot['confidence'],
                        ratings_state.get('rating_version', 'elo_v1'),
                        self._coerce_datetime(snapshot['last_match_at']),
                        Json(snapshot.get('metadata') or {}),
                    ),
                )
        self.db_conn.commit()

    def rebuild_ratings(self, force_refresh: bool = False) -> Dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        if (
            not force_refresh
            and self._cache is not None
            and self._cache_created_at is not None
            and now_utc - self._cache_created_at < CACHE_TTL
        ):
            return self._cache

        rows = self._query_match_rows()
        ratings_state = self.build_ratings_from_rows(rows)
        try:
            self._persist_ratings(ratings_state)
        except Exception as exc:
            logger.warning(f"⚠️  Could not persist team ratings: {exc}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass
        self._cache = ratings_state
        self._cache_created_at = now_utc
        return ratings_state

    def get_matchup_context(
        self,
        team_a: str,
        team_b: str,
        event_name: str = '',
        pick_team: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        team_a_key = canonicalize_team_name(team_a)
        team_b_key = canonicalize_team_name(team_b)
        if not team_a_key or not team_b_key or team_a_key == team_b_key:
            return None

        ratings_state = self.rebuild_ratings(force_refresh=force_refresh)
        teams = ratings_state.get('teams', {})
        snapshot_a = teams.get(team_a_key)
        snapshot_b = teams.get(team_b_key)
        snapshot_a = snapshot_a or {
            'team_key': team_a_key,
            'team_name': display_team_name(team_a, team_a_key),
            'rating': BASE_RATING,
            'matches_played': 0,
            'confidence': 0.15,
            'confidence_label': 'low',
        }
        snapshot_b = snapshot_b or {
            'team_key': team_b_key,
            'team_name': display_team_name(team_b, team_b_key),
            'rating': BASE_RATING,
            'matches_played': 0,
            'confidence': 0.15,
            'confidence_label': 'low',
        }

        raw_rating_a = float(snapshot_a['rating'])
        raw_rating_b = float(snapshot_b['rating'])
        team_a_confidence = max(0.2, float(snapshot_a.get('confidence', 0.15) or 0.15))
        team_b_confidence = max(0.2, float(snapshot_b.get('confidence', 0.15) or 0.15))
        rating_a = BASE_RATING + ((raw_rating_a - BASE_RATING) * team_a_confidence)
        rating_b = BASE_RATING + ((raw_rating_b - BASE_RATING) * team_b_confidence)
        expected_a = _expected_score(rating_a, rating_b)
        expected_b = 1.0 - expected_a
        team_a_pct = int(round(expected_a * 100))
        team_b_pct = 100 - team_a_pct
        gap = round(abs(rating_a - rating_b), 1)
        favorite_key = team_a_key if rating_a >= rating_b else team_b_key
        favorite_name = snapshot_a['team_name'] if favorite_key == team_a_key else snapshot_b['team_name']
        favorite_prob = max(expected_a, expected_b)
        confidence = round(min(0.95, (float(snapshot_a.get('confidence', 0.15)) + float(snapshot_b.get('confidence', 0.15))) / 2.0), 2)
        confidence_label = _confidence_label(confidence)

        if gap < 12:
            summary_line = (
                f"In-house rating prices this close: {snapshot_a['team_name']} {team_a_pct}% "
                f"vs {snapshot_b['team_name']} {team_b_pct}%."
            )
            tweet_line = f"In-house: close price, {snapshot_a['team_name']} {team_a_pct}% fair"
        else:
            summary_line = (
                f"In-house rating has {favorite_name} +{int(round(gap))} over the other side. "
                f"Fair line: {snapshot_a['team_name']} {team_a_pct}% vs {snapshot_b['team_name']} {team_b_pct}%."
            )
            tweet_line = f"In-house: {favorite_name} +{int(round(gap))}, fair {max(team_a_pct, team_b_pct)}%"

        pick_key = canonicalize_team_name(pick_team) if pick_team else None
        pick_line = None
        pick_pct = None
        if pick_key in {team_a_key, team_b_key}:
            pick_name = snapshot_a['team_name'] if pick_key == team_a_key else snapshot_b['team_name']
            pick_prob = expected_a if pick_key == team_a_key else expected_b
            pick_pct = int(round(pick_prob * 100))
            if gap < 20:
                pick_line = f"In-house prices {pick_name} at {pick_pct}% fair"
            elif pick_key == favorite_key:
                pick_line = f"In-house agrees: {pick_name} {pick_pct}% fair"
            else:
                pick_line = f"In-house is against it: {pick_name} {pick_pct}% fair"

        evidence_lines = [
            f"In-house board: {snapshot_a['team_name']} {rating_a:.0f}, {snapshot_b['team_name']} {rating_b:.0f} after confidence shrink.",
            f"Fair line: {snapshot_a['team_name']} {team_a_pct}% vs {snapshot_b['team_name']} {team_b_pct}%.",
            f"Confidence is {confidence_label} from {snapshot_a.get('matches_played', 0)} and {snapshot_b.get('matches_played', 0)} rated matches.",
        ]
        if pick_line:
            evidence_lines.insert(1, pick_line + '.')
        if event_name:
            evidence_lines.append(f"Event context: {event_name}.")

        return {
            'rating_version': ratings_state.get('rating_version', 'elo_v1'),
            'team_a': snapshot_a['team_name'],
            'team_b': snapshot_b['team_name'],
            'team_a_key': team_a_key,
            'team_b_key': team_b_key,
            'team_a_rating': round(rating_a, 1),
            'team_b_rating': round(rating_b, 1),
            'team_a_raw_rating': round(raw_rating_a, 1),
            'team_b_raw_rating': round(raw_rating_b, 1),
            'team_a_win_probability': round(expected_a, 3),
            'team_b_win_probability': round(expected_b, 3),
            'team_a_win_probability_pct': team_a_pct,
            'team_b_win_probability_pct': team_b_pct,
            'favorite': favorite_name,
            'favorite_key': favorite_key,
            'favorite_win_probability': round(favorite_prob, 3),
            'favorite_win_probability_pct': int(round(favorite_prob * 100)),
            'rating_gap': gap,
            'confidence': confidence,
            'confidence_label': confidence_label,
            'team_a_matches': snapshot_a.get('matches_played', 0),
            'team_b_matches': snapshot_b.get('matches_played', 0),
            'summary_line': summary_line,
            'tweet_line': tweet_line,
            'pick_line': pick_line,
            'pick_team': display_team_name(pick_team, pick_key) if pick_key else None,
            'pick_team_key': pick_key,
            'pick_win_probability_pct': pick_pct,
            'event': event_name,
            'evidence_lines': evidence_lines[:4],
        }


_engine = None


def get_team_rating_engine() -> TeamRatingEngine:
    global _engine
    if _engine is None:
        _engine = TeamRatingEngine()
    return _engine