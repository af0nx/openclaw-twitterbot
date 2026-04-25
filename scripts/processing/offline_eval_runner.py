#!/usr/bin/env python3
"""Replay recent events through the generator and score outputs offline."""

from __future__ import annotations

import argparse
import json
import logging
import os
from difflib import SequenceMatcher
from pathlib import Path
from statistics import mean
from typing import Any

import psycopg2
from dotenv import load_dotenv

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.content_generator import ContentGenerator, normalize_generated_text
from processing.fact_checker import get_fact_checker
from processing.tone_validator import get_tone_validator


load_dotenv('/dev/shm/.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


PILLAR_MAP = {
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
}


class OfflineEvalRunner:
    def __init__(self):
        self.db_conn = None
        self.generator = ContentGenerator()
        self.fact_checker = get_fact_checker()
        self.tone_validator = get_tone_validator()

    def connect_db(self):
        self.db_conn = psycopg2.connect(os.getenv('DATABASE_URL'))
        self.generator.connect_db()

    def fetch_events(self, days: int, limit: int, include_engagement: bool) -> list[dict[str, Any]]:
        categories_filter = ""
        if not include_engagement:
            categories_filter = "AND e.source != 'engagement_engine'"

        with self.db_conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT e.id, e.headline, e.content, e.category, e.urgency, e.metadata,
                       COALESCE(t.content, '') AS posted_content
                FROM twitter_bot.events e
                LEFT JOIN LATERAL (
                    SELECT content
                    FROM twitter_bot.tweets_v2
                    WHERE event_id = e.id AND status = 'posted'
                    ORDER BY posted_at DESC NULLS LAST, created_at DESC
                    LIMIT 1
                ) t ON TRUE
                WHERE e.created_at > NOW() - (%s || ' days')::interval
                  AND e.category != 'match_prediction'
                  {categories_filter}
                ORDER BY e.created_at DESC
                LIMIT %s
                """,
                (str(days), limit),
            )
            rows = cur.fetchall()

        events = []
        for row in rows:
            events.append({
                'id': str(row[0]),
                'headline': row[1],
                'content': row[2],
                'category': row[3],
                'urgency': row[4],
                'metadata': row[5],
                'posted_content': row[6],
            })
        return events

    @staticmethod
    def _novelty_score(candidate: str, reference: str) -> float:
        if not reference:
            return 1.0
        return max(0.0, 1.0 - SequenceMatcher(None, candidate.lower(), reference.lower()).ratio())

    @staticmethod
    def _length_score(text: str) -> float:
        return max(0.0, 1.0 - abs(len(text) - 110) / 170)

    def evaluate_event(self, event: dict[str, Any]) -> dict[str, Any]:
        pillar = PILLAR_MAP.get(event.get('category'), 1)
        generation = self.generator.dual_agent_generate(event, pillar)
        text = normalize_generated_text(generation.get('final_text', ''))
        fact = self.fact_checker.check(text, event)
        tone = self.tone_validator.validate(text, pillar)
        novelty = self._novelty_score(text, event.get('posted_content', ''))
        length = self._length_score(text)
        score = (
            (40.0 if fact.get('valid') else 0.0)
            + float(tone.get('tone_score', tone.get('score', 0))) * 4.0
            + novelty * 20.0
            + length * 10.0
        )
        return {
            'event_id': event['id'],
            'category': event.get('category'),
            'headline': event.get('headline', ''),
            'candidate_text': text,
            'fact_valid': fact.get('valid', True),
            'fact_issues': fact.get('issues', []),
            'tone_valid': tone.get('valid', True),
            'tone_score': tone.get('tone_score', tone.get('score', 0)),
            'novelty_score': round(novelty, 3),
            'length_score': round(length, 3),
            'offline_score': round(score, 2),
            'model': generation.get('model', 'unknown'),
        }

    def run(self, days: int, limit: int, include_engagement: bool) -> list[dict[str, Any]]:
        self.connect_db()
        events = self.fetch_events(days=days, limit=limit, include_engagement=include_engagement)
        results = []
        for event in events:
            try:
                results.append(self.evaluate_event(event))
            except Exception as e:
                logger.warning(f"⚠️  Offline eval failed for {event['id']}: {e}")
        return results


def main() -> int:
    parser = argparse.ArgumentParser(description='Replay recent events through the generator and score them offline')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--include-engagement', action='store_true')
    parser.add_argument('--json-output', default='')
    args = parser.parse_args()

    runner = OfflineEvalRunner()
    results = runner.run(days=args.days, limit=args.limit, include_engagement=args.include_engagement)

    if not results:
        print('No results')
        return 0

    avg_score = mean(result['offline_score'] for result in results)
    print(f"offline_eval events={len(results)} avg_score={avg_score:.2f}")
    for result in results:
        print(
            f"{result['offline_score']:6.2f} | {result['category']:<24} | "
            f"fact={'Y' if result['fact_valid'] else 'N'} | tone={result['tone_score']:>2} | "
            f"{result['candidate_text'][:110]}"
        )

    if args.json_output:
        Path(args.json_output).write_text(json.dumps(results, indent=2))
        print(f"wrote {args.json_output}")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())