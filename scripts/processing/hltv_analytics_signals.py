#!/usr/bin/env python3
"""Lightweight HLTV pre-match analytics parsing.

HLTV's static betting analytics page exposes a usable summary-insights block, but
the visible best-bet odds rows currently render placeholder values across
unrelated matches. This module uses only the trustworthy summary text as a
low-weight qualitative signal.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup, Tag


_H2H_RE = re.compile(
    r"played\s+\d+\s+matches?\s+against\s+(.+?)\s+in the past 30 days,\s*they won\s+(\d+)\s+and lost\s+(\d+)",
    re.IGNORECASE,
)
_RANK_RE = re.compile(r"(better|worse) ranked\s*\(#?(\d+)\)", re.IGNORECASE)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _team_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _dedupe(lines: List[str]) -> List[str]:
    seen = set()
    output: List[str] = []
    for line in lines:
        cleaned = _clean_text(line)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
    return output


def _prefix_reasons(team_name: str, reasons: List[str]) -> List[str]:
    return [f"{team_name} {reason}" for reason in _dedupe(reasons)]


def _score_reason(text: str) -> float:
    lower = text.lower()
    score = 0.6
    if 'better form ranking' in lower or 'better form' in lower:
        score += 0.9
    if 'better ranked' in lower or 'worse ranked' in lower:
        score += 0.8
    if 'won the last match against' in lower:
        score += 0.5

    h2h_match = _H2H_RE.search(text)
    if h2h_match:
        wins = int(h2h_match.group(2))
        losses = int(h2h_match.group(3))
        score += min(1.0, abs(wins - losses) * 0.35)

    return round(score, 2)


def _short_reason(text: str, team_name: str) -> str:
    cleaned = _clean_text(text).rstrip('.')
    lowered = cleaned.lower()
    team_prefix = f"{team_name} ".lower()
    if lowered.startswith(team_prefix):
        cleaned = cleaned[len(team_name) + 1:]
        lowered = cleaned.lower()

    h2h_match = _H2H_RE.search(cleaned)
    if h2h_match:
        opponent_name = _clean_text(h2h_match.group(1))
        wins = h2h_match.group(2)
        losses = h2h_match.group(3)
        return f"recent H2H {wins}-{losses} vs {opponent_name}"

    rank_match = _RANK_RE.search(cleaned)
    if rank_match:
        direction = rank_match.group(1).lower()
        rank = rank_match.group(2)
        if direction == 'better':
            return f"better rank (#{rank})"
        return f"worse rank (#{rank})"

    replacements = {
        'has better form ranking': 'better recent form',
        'better form ranking': 'better recent form',
        'won the last match against': 'won last meeting vs',
    }
    for original, replacement in replacements.items():
        if original in lowered:
            return re.sub(original, replacement, cleaned, flags=re.IGNORECASE)

    return cleaned


def _parse_team_container(container: Tag) -> Optional[Dict[str, Any]]:
    team_name_node = container.select_one('.team-name')
    team_name = _clean_text(team_name_node.get_text(' ', strip=True) if team_name_node else '')
    if not team_name:
        return None

    favor_lines: List[str] = []
    against_lines: List[str] = []
    current_bucket: Optional[str] = None

    for child in container.find_all(recursive=False):
        classes = set(child.get('class') or [])
        if 'favor' in classes:
            heading = _clean_text(child.get_text(' ', strip=True)).lower()
            if 'in favor' in heading:
                current_bucket = 'favor'
            elif 'against' in heading:
                current_bucket = 'against'
            else:
                current_bucket = None
            continue

        if 'analytics-insights-insight' not in classes or not current_bucket:
            continue

        info = child.select_one('.analytics-insights-info')
        text = _clean_text(info.get_text(' ', strip=True) if info else '')
        if not text or 'no insights detected' in text.lower():
            continue

        if current_bucket == 'favor':
            favor_lines.append(text)
        else:
            against_lines.append(text)

    favor_lines = _dedupe(favor_lines)
    against_lines = _dedupe(against_lines)
    favor_reasons = [_short_reason(line, team_name) for line in favor_lines]
    against_reasons = [_short_reason(line, team_name) for line in against_lines]

    signal_score = round(
        sum(_score_reason(line) for line in favor_lines)
        - sum(_score_reason(line) for line in against_lines),
        2,
    )

    return {
        'team_name': team_name,
        'team_key': _team_key(team_name),
        'favor_lines': favor_lines,
        'against_lines': against_lines,
        'favor_reasons': favor_reasons,
        'against_reasons': against_reasons,
        'signal_score': signal_score,
    }


def parse_hltv_analytics_context(
    html: str,
    *,
    team_a: str,
    team_b: str,
    pick_team: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not html:
        return None

    try:
        soup = BeautifulSoup(html, 'lxml')
    except Exception:
        try:
            soup = BeautifulSoup(html, 'html.parser')
        except Exception:
            return None
    containers = soup.find_all(class_='analytics-insights-container', limit=2)
    if len(containers) < 2:
        return None

    parsed_a = _parse_team_container(containers[0])
    parsed_b = _parse_team_container(containers[1])
    if not parsed_a or not parsed_b:
        return None

    # Trust the fixture team names for downstream matching, but keep the parsed
    # order from the page because the summary blocks are laid out team1/team2.
    parsed_a['team_name'] = team_a or parsed_a['team_name']
    parsed_a['team_key'] = _team_key(parsed_a['team_name'])
    parsed_b['team_name'] = team_b or parsed_b['team_name']
    parsed_b['team_key'] = _team_key(parsed_b['team_name'])

    odds_texts = [
        _clean_text(node.get_text(' ', strip=True))
        for node in soup.find_all(class_='best-bet-odds', limit=8)
    ]
    numeric_odds = [text for text in odds_texts if re.fullmatch(r"\d+(?:\.\d+)?", text)]
    providers = _dedupe([
        _clean_text(node.get_text(' ', strip=True))
        for node in soup.find_all(class_='best-bet-provider', limit=8)
    ])
    market_reference_status = 'unavailable'
    if len(numeric_odds) >= 4 and set(numeric_odds) == {'1.23'}:
        market_reference_status = 'placeholder'
    elif numeric_odds:
        market_reference_status = 'present'

    score_delta = round(parsed_a['signal_score'] - parsed_b['signal_score'], 2)
    if score_delta > 0.5:
        signal_leader = parsed_a['team_name']
        leader_reasons = _dedupe(
            _prefix_reasons(parsed_a['team_name'], parsed_a['favor_reasons'])
            + _prefix_reasons(parsed_b['team_name'], parsed_b['against_reasons'])
        )[:3]
    elif score_delta < -0.5:
        signal_leader = parsed_b['team_name']
        leader_reasons = _dedupe(
            _prefix_reasons(parsed_b['team_name'], parsed_b['favor_reasons'])
            + _prefix_reasons(parsed_a['team_name'], parsed_a['against_reasons'])
        )[:3]
    else:
        signal_leader = None
        leader_reasons = []

    if signal_leader and leader_reasons:
        summary_line = f"HLTV summary leans {signal_leader}: {', '.join(leader_reasons[:2])}."
    elif signal_leader:
        summary_line = f"HLTV summary leans {signal_leader}."
    else:
        summary_line = "HLTV summary looks roughly split."

    context: Dict[str, Any] = {
        'source': 'hltv_analytics_summary',
        'summary_line': summary_line,
        'team_a': parsed_a['team_name'],
        'team_b': parsed_b['team_name'],
        'team_a_score': parsed_a['signal_score'],
        'team_b_score': parsed_b['signal_score'],
        'team_a_favor_reasons': parsed_a['favor_reasons'],
        'team_a_against_reasons': parsed_a['against_reasons'],
        'team_b_favor_reasons': parsed_b['favor_reasons'],
        'team_b_against_reasons': parsed_b['against_reasons'],
        'signal_score_delta': score_delta,
        'signal_leader': signal_leader,
        'leader_reasons': leader_reasons,
        'market_reference_status': market_reference_status,
        'quoted_provider_names': providers[:4],
        'quoted_best_bet_odds': numeric_odds[:8],
        'evidence_lines': [summary_line],
    }

    if market_reference_status == 'placeholder':
        context['evidence_lines'].append(
            'HLTV static best-bet rows looked placeholder (1.23 / No bet), so they were ignored.'
        )

    pick_key = _team_key(pick_team or '')
    if pick_key in {parsed_a['team_key'], parsed_b['team_key']}:
        pick_context = parsed_a if pick_key == parsed_a['team_key'] else parsed_b
        opponent_context = parsed_b if pick_context is parsed_a else parsed_a
        context.update(
            {
                'pick_team': pick_context['team_name'],
                'opponent_team': opponent_context['team_name'],
                'pick_score': pick_context['signal_score'],
                'opponent_score': opponent_context['signal_score'],
                'pick_signal_delta': round(
                    pick_context['signal_score'] - opponent_context['signal_score'],
                    2,
                ),
                'pick_support_reasons': _dedupe(
                    _prefix_reasons(pick_context['team_name'], pick_context['favor_reasons'])
                    + _prefix_reasons(opponent_context['team_name'], opponent_context['against_reasons'])
                ),
                'pick_risk_reasons': _dedupe(
                    _prefix_reasons(pick_context['team_name'], pick_context['against_reasons'])
                    + _prefix_reasons(opponent_context['team_name'], opponent_context['favor_reasons'])
                ),
            }
        )

    return context
