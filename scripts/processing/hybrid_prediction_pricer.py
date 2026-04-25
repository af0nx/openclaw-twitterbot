#!/usr/bin/env python3
"""Hybrid pre-match pricing for match-winner predictions.

Market implied probability is the anchor. The in-house rating prior only nudges
the final fair line because the underlying historical sample is still sparse and
confidence-shrunk.
"""

from typing import Any, Dict, Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.team_rating_engine import (
    TeamRatingEngine,
    canonicalize_team_name,
    display_team_name,
    get_team_rating_engine,
)


MODEL_VERSION = 'market_rating_v1'
SUPPORTED_MARKETS = {'match_winner'}


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == '':
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def decimal_odds_to_implied_probability(decimal_odds: Any) -> Optional[float]:
    odds = _safe_float(decimal_odds)
    if odds is None or odds <= 1.01:
        return None
    return round(_clamp(1.0 / odds, 0.05, 0.95), 3)


class HybridPredictionPricer:
    """Blend market baseline with the in-house rating prior."""

    def __init__(self, rating_engine: Optional[TeamRatingEngine] = None):
        self.rating_engine = rating_engine or get_team_rating_engine()

    def connect_db(self, conn=None):
        self.rating_engine.connect_db(conn)

    @staticmethod
    def _rating_weight(rating_context: Dict[str, Any]) -> float:
        confidence = _safe_float(rating_context.get('confidence')) or 0.0
        confidence = _clamp(confidence, 0.0, 1.0)
        rating_gap = abs(_safe_float(rating_context.get('rating_gap')) or 0.0)
        min_matches = min(
            int(rating_context.get('team_a_matches', 0) or 0),
            int(rating_context.get('team_b_matches', 0) or 0),
        )

        weight = 0.12 + (confidence * 0.18)
        weight += min(0.08, rating_gap / 300.0)
        weight += min(0.05, min_matches / 50.0)
        return round(_clamp(weight, 0.15, 0.42), 2)

    @staticmethod
    def _resolve_analytics_adjustment(
        analytics_context: Optional[Dict[str, Any]],
        pick_name: str,
        market_prob: Optional[float],
    ) -> Dict[str, Any]:
        if not isinstance(analytics_context, dict):
            return {
                'adjustment': 0.0,
                'signal_delta': None,
                'alignment': None,
                'line': None,
                'note': None,
            }

        signal_delta = _safe_float(analytics_context.get('pick_signal_delta'))
        if signal_delta is None:
            return {
                'adjustment': 0.0,
                'signal_delta': None,
                'alignment': None,
                'line': None,
                'note': None,
            }

        max_adjustment = 0.02 if market_prob is not None else 0.03
        adjustment = round(_clamp(signal_delta * 0.006, -max_adjustment, max_adjustment), 3)
        support_reasons = analytics_context.get('pick_support_reasons') or []
        risk_reasons = analytics_context.get('pick_risk_reasons') or []

        alignment = 'neutral'
        line = None
        if adjustment >= 0.005:
            alignment = 'supportive'
            reason_text = ', '.join(str(reason) for reason in support_reasons[:2])
            line = f"HLTV summary backs {pick_name}"
            if reason_text:
                line += f": {reason_text}"
        elif adjustment <= -0.005:
            alignment = 'contrarian'
            reason_text = ', '.join(str(reason) for reason in risk_reasons[:2])
            line = f"HLTV summary pushes back on {pick_name}"
            if reason_text:
                line += f": {reason_text}"

        note = None
        if analytics_context.get('market_reference_status') == 'placeholder':
            note = (
                'Ignored HLTV static best-bet odds because the page only exposed '
                'placeholder 1.23 / No bet rows.'
            )

        return {
            'adjustment': adjustment,
            'signal_delta': round(signal_delta, 2),
            'alignment': alignment,
            'line': line,
            'note': note,
        }

    def price_prediction(
        self,
        *,
        team_a: str,
        team_b: str,
        pick_team: str,
        market_type: str = 'match_winner',
        pick_odds: Any = None,
        provider_win_probability: Any = None,
        analytics_context: Optional[Dict[str, Any]] = None,
        event_name: str = '',
        force_refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        market_key = str(market_type or 'match_winner').strip().lower()
        if market_key not in SUPPORTED_MARKETS:
            return None

        team_a_key = canonicalize_team_name(team_a)
        team_b_key = canonicalize_team_name(team_b)
        pick_key = canonicalize_team_name(pick_team)
        if not team_a_key or not team_b_key or pick_key not in {team_a_key, team_b_key}:
            return None

        rating_context = self.rating_engine.get_matchup_context(
            team_a=team_a,
            team_b=team_b,
            event_name=event_name,
            pick_team=pick_team,
            force_refresh=force_refresh,
        )
        if not rating_context:
            return None

        pick_name = display_team_name(pick_team, pick_key)
        rating_prob = float(
            rating_context.get('team_a_win_probability')
            if pick_key == team_a_key
            else rating_context.get('team_b_win_probability')
        )
        rating_pct = int(round(rating_prob * 100))
        market_prob = decimal_odds_to_implied_probability(pick_odds)
        provider_prob = _safe_float(provider_win_probability)
        if provider_prob is not None:
            provider_prob = round(_clamp(provider_prob, 0.01, 0.99), 3)

        rating_weight = 1.0
        market_weight = 0.0
        signed_edge_pct = None

        if market_prob is None:
            hybrid_prob = rating_prob
            verdict = 'rating_only'
            pick_line = f"Hybrid fallback: {pick_name} {rating_pct}% fair off in-house rating"
            summary_line = f"Hybrid fallback uses only the in-house number: {pick_name} {rating_pct}% fair."
        else:
            rating_weight = self._rating_weight(rating_context)
            market_weight = round(1.0 - rating_weight, 2)
            hybrid_prob = _clamp(
                (market_prob * market_weight) + (rating_prob * rating_weight),
                0.05,
                0.95,
            )
            summary_line = (
                f"Hybrid price blends market {int(round(market_prob * 100))}% with in-house {rating_pct}% "
                f"before final context adjustments."
            )

        analytics_adjustment = self._resolve_analytics_adjustment(
            analytics_context,
            pick_name,
            market_prob,
        )
        if analytics_adjustment['adjustment']:
            hybrid_prob = _clamp(hybrid_prob + analytics_adjustment['adjustment'], 0.05, 0.95)

        hybrid_prob = round(hybrid_prob, 3)
        hybrid_pct = int(round(hybrid_prob * 100))
        fair_odds = round(1.0 / hybrid_prob, 2) if hybrid_prob > 0 else None

        if market_prob is None:
            if abs(analytics_adjustment['adjustment']) >= 0.01:
                verdict = 'rating_plus_hltv'
                if analytics_adjustment['adjustment'] > 0:
                    pick_line = f"In-house + HLTV agree on {pick_name}: {hybrid_pct}% fair"
                    summary_line = (
                        f"In-house pricing starts at {rating_pct}% and HLTV summary nudges {pick_name} "
                        f"to {hybrid_pct}% fair."
                    )
                else:
                    pick_line = f"In-house leans {pick_name}, but HLTV trims it to {hybrid_pct}% fair"
                    summary_line = (
                        f"In-house pricing starts at {rating_pct}% and HLTV summary trims {pick_name} "
                        f"to {hybrid_pct}% fair."
                    )
            else:
                verdict = 'rating_only'
                pick_line = f"Hybrid fallback: {pick_name} {hybrid_pct}% fair off in-house rating"
                summary_line = f"Hybrid fallback uses only the in-house number: {pick_name} {hybrid_pct}% fair."
        else:
            signed_edge_pct = round((hybrid_prob - market_prob) * 100.0, 1)
            market_pct = int(round(market_prob * 100))

            if signed_edge_pct >= 3.0:
                verdict = 'value'
                pick_line = f"Hybrid line likes {pick_name}: {hybrid_pct}% fair vs {market_pct}% market"
            elif signed_edge_pct <= -3.0:
                verdict = 'fade'
                pick_line = f"Hybrid line fades {pick_name}: {hybrid_pct}% fair vs {market_pct}% market"
            else:
                verdict = 'close'
                pick_line = f"Hybrid line is close: {pick_name} {hybrid_pct}% fair vs {market_pct}% market"

            summary_line = (
                f"Hybrid price blends market {market_pct}% with in-house {rating_pct}% "
                f"to land {pick_name} at {hybrid_pct}% fair."
            )

        provider_alignment = None
        provider_delta_pct = None
        provider_line = None
        if provider_prob is not None:
            provider_delta_pct = round((provider_prob - hybrid_prob) * 100.0, 1)
            if abs(provider_delta_pct) < 2.5:
                provider_alignment = 'aligned'
            elif provider_delta_pct > 0:
                provider_alignment = 'provider_higher'
                provider_line = (
                    f"Provider runs {int(round(abs(provider_delta_pct)))} pts hotter on {pick_name}"
                )
            else:
                provider_alignment = 'provider_lower'
                provider_line = (
                    f"Provider runs {int(round(abs(provider_delta_pct)))} pts lower on {pick_name}"
                )

        evidence_lines = [summary_line]
        if market_prob is not None:
            evidence_lines.append(
                f"Fair odds {fair_odds:.2f}, edge vs market {signed_edge_pct:+.1f} pts."
            )
            evidence_lines.append(
                f"Blend weights: market {int(round(market_weight * 100))}% / "
                f"ratings {int(round(rating_weight * 100))}%"
            )
        else:
            evidence_lines.append(
                f"No usable market price attached, so this falls back to the rating prior only."
            )
        if analytics_adjustment['line']:
            evidence_lines.append(analytics_adjustment['line'] + '.')
        if analytics_adjustment['note']:
            evidence_lines.append(analytics_adjustment['note'])
        if provider_line:
            evidence_lines.append(provider_line + '.')

        return {
            'model_version': 'market_rating_hltv_v2',
            'market_type': market_key,
            'pick_team': pick_name,
            'pick_team_key': pick_key,
            'market_implied_probability': market_prob,
            'market_implied_probability_pct': int(round(market_prob * 100)) if market_prob is not None else None,
            'rating_win_probability': round(rating_prob, 3),
            'rating_win_probability_pct': rating_pct,
            'hybrid_win_probability': hybrid_prob,
            'hybrid_win_probability_pct': hybrid_pct,
            'fair_odds': fair_odds,
            'signed_edge_pct': signed_edge_pct,
            'display_edge_pct': round(max(0.0, signed_edge_pct or 0.0), 1),
            'market_weight': market_weight,
            'rating_weight': rating_weight,
            'provider_win_probability': provider_prob,
            'provider_win_probability_pct': int(round(provider_prob * 100)) if provider_prob is not None else None,
            'provider_delta_pct': provider_delta_pct,
            'provider_alignment': provider_alignment,
            'analytics_adjustment_pct': analytics_adjustment['adjustment'],
            'analytics_signal_delta': analytics_adjustment['signal_delta'],
            'analytics_alignment': analytics_adjustment['alignment'],
            'verdict': verdict,
            'summary_line': summary_line,
            'tweet_line': pick_line,
            'pick_line': pick_line,
            'provider_line': provider_line,
            'analytics_line': analytics_adjustment['line'],
            'rating_context': rating_context,
            'evidence_lines': evidence_lines[:4],
        }


_pricer = None


def get_hybrid_prediction_pricer() -> HybridPredictionPricer:
    global _pricer
    if _pricer is None:
        _pricer = HybridPredictionPricer()
    return _pricer