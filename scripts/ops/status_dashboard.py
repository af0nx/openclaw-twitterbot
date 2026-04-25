#!/usr/bin/env python3
"""Small operational dashboard for the CS2 bot."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


load_dotenv('/dev/shm/.env')


def fetch_one(cur, query: str, params=()):
    cur.execute(query, params)
    return cur.fetchone()


def build_dashboard(conn) -> dict:
    dashboard: dict = {}
    with conn.cursor() as cur:
        row = fetch_one(
            cur,
            """
            SELECT COUNT(*) FILTER (WHERE status = 'queued'),
                   COUNT(*) FILTER (WHERE status = 'hitl_pending'),
                   COUNT(*) FILTER (WHERE status = 'draft')
            FROM twitter_bot.tweets_v2
            WHERE created_at > NOW() - INTERVAL '7 days'
            """,
        )
        dashboard['queue_depth'] = {
            'queued': int(row[0] or 0),
            'hitl_pending': int(row[1] or 0),
            'draft': int(row[2] or 0),
        }

        row = fetch_one(
            cur,
            """
            SELECT COUNT(*) FILTER (WHERE has_media),
                   COUNT(*) FILTER (
                       WHERE has_media
                         AND COALESCE(NULLIF(BTRIM(ocr_extracted_text), ''), '') NOT IN ('', '__no_text__')
                   ),
                   COUNT(*) FILTER (WHERE has_media AND ocr_extracted_text = '__no_text__')
            FROM twitter_bot.siftly_events
            WHERE created_at > NOW() - INTERVAL '30 days'
            """,
        )
        total_media = int(row[0] or 0)
        ocr_ok = int(row[1] or 0)
        dashboard['ocr'] = {
            'media_rows': total_media,
            'ocr_hit_rate': round((ocr_ok / total_media) * 100, 2) if total_media else 0.0,
            'no_text_rows': int(row[2] or 0),
        }

        row = fetch_one(
            cur,
            """
            SELECT COUNT(*), COUNT(*) FILTER (WHERE media_path IS NOT NULL)
            FROM twitter_bot.tweets_v2
            WHERE status = 'posted'
              AND posted_at > NOW() - INTERVAL '30 days'
            """,
        )
        total_posted = int(row[0] or 0)
        with_media = int(row[1] or 0)
        dashboard['media_attach'] = {
            'posted': total_posted,
            'with_media': with_media,
            'attach_rate': round((with_media / total_posted) * 100, 2) if total_posted else 0.0,
        }

        row = fetch_one(
            cur,
            """
            SELECT COUNT(*) FILTER (WHERE auto_approved),
                   COUNT(*) FILTER (WHERE hitl_requested_at IS NOT NULL),
                   COUNT(*) FILTER (WHERE hitl_approved_at IS NOT NULL)
            FROM twitter_bot.tweets_v2
            WHERE created_at > NOW() - INTERVAL '30 days'
            """,
        )
        dashboard['approval'] = {
            'auto_approved': int(row[0] or 0),
            'hitl_requested': int(row[1] or 0),
            'hitl_approved': int(row[2] or 0),
        }

        bandit_min_samples = int(os.getenv('GENERATOR_TEMPLATE_MIN_SAMPLES', '3'))
        row = fetch_one(
            cur,
            """
            SELECT COUNT(*) FILTER (
                       WHERE source = 'engagement_engine'
                         AND metadata IS NOT NULL
                         AND metadata ? 'template_key'
                         AND created_at > NOW() - INTERVAL '7 days'
                   ),
                   COUNT(*) FILTER (
                       WHERE source = 'engagement_engine'
                         AND metadata IS NOT NULL
                         AND metadata ? 'template_key'
                         AND created_at > NOW() - INTERVAL '30 days'
                   )
            FROM twitter_bot.events
            WHERE created_at > NOW() - INTERVAL '30 days'
            """,
        )
        assigned_7d = int(row[0] or 0)
        assigned_30d = int(row[1] or 0)

        row = fetch_one(
            cur,
            """
            SELECT COUNT(*)
            FROM twitter_bot.tweets_v2 t
            JOIN twitter_bot.events e ON e.id = t.event_id
            WHERE t.status = 'posted'
              AND t.posted_at > NOW() - INTERVAL '30 days'
              AND e.metadata IS NOT NULL
              AND e.metadata ? 'template_key'
            """,
        )
        posted_template_tweets = int(row[0] or 0)

        row = fetch_one(
            cur,
            """
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE tweet_count >= %s),
                   COALESCE(SUM(tweet_count), 0)
            FROM twitter_bot.mv_generator_template_30d
            """,
            (bandit_min_samples,),
        )
        scorecard_rows = int(row[0] or 0)
        ready_templates = int(row[1] or 0)
        tracked_template_posts = int(row[2] or 0)

        cur.execute(
            """
            SELECT strategy,
                   COUNT(*)::INT AS templates_seen,
                   COUNT(*) FILTER (WHERE tweet_count >= %s)::INT AS ready_templates,
                   COALESCE(SUM(tweet_count), 0)::INT AS tweet_count,
                   AVG(avg_er) AS avg_er
            FROM twitter_bot.mv_generator_template_30d
            GROUP BY strategy
            ORDER BY tweet_count DESC, avg_er DESC NULLS LAST
            LIMIT 8
            """,
            (bandit_min_samples,),
        )
        dashboard['bandit'] = {
            'config': {
                'generator_min_er': float(os.getenv('GENERATOR_MIN_ER', '0.003')),
                'trial_budget': int(os.getenv('GENERATOR_TEMPLATE_TRIAL_BUDGET', '3')),
                'template_min_samples': bandit_min_samples,
                'template_retire_er': float(os.getenv('GENERATOR_TEMPLATE_RETIRE_ER', '0.003')),
                'epsilon': float(os.getenv('GENERATOR_TEMPLATE_EPSILON', '0.15')),
            },
            'assigned_template_events_7d': assigned_7d,
            'assigned_template_events_30d': assigned_30d,
            'posted_template_tweets_30d': posted_template_tweets,
            'tracked_template_posts_30d': tracked_template_posts,
            'scorecard_rows_30d': scorecard_rows,
            'templates_ready_for_tuning': ready_templates,
            'ready_to_tune': ready_templates >= 3 and posted_template_tweets >= (bandit_min_samples * 3),
            'by_strategy': [
                {
                    'strategy': row[0],
                    'templates_seen': int(row[1] or 0),
                    'ready_templates': int(row[2] or 0),
                    'tweet_count': int(row[3] or 0),
                    'avg_er': round(float(row[4] or 0), 4),
                }
                for row in cur.fetchall()
            ],
        }

        cur.execute(
            """
            SELECT category, strategy, template_key, tweet_count, avg_er
            FROM twitter_bot.mv_generator_template_30d
            ORDER BY avg_er DESC NULLS LAST, tweet_count DESC
            LIMIT 8
            """
        )
        dashboard['generator_scorecards'] = [
            {
                'category': row[0],
                'strategy': row[1],
                'template_key': row[2],
                'tweet_count': int(row[3] or 0),
                'avg_er': round(float(row[4] or 0), 4),
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT category, card_type, variant, tweet_count, avg_er
            FROM twitter_bot.mv_media_experiment_30d
            ORDER BY tweet_count DESC, avg_er DESC NULLS LAST
            LIMIT 8
            """
        )
        dashboard['media_experiments'] = [
            {
                'category': row[0],
                'card_type': row[1],
                'variant': row[2],
                'tweet_count': int(row[3] or 0),
                'avg_er': round(float(row[4] or 0), 4),
            }
            for row in cur.fetchall()
        ]

        dashboard['service_last_success'] = {}
        service_queries = {
            'tweet_scheduler': "SELECT MAX(processed_at) FROM twitter_bot.events",
            'twitter_poster': "SELECT MAX(posted_at) FROM twitter_bot.tweets_v2",
            'engagement_tracker': "SELECT MAX(created_at) FROM twitter_bot.engagement_tracking",
            'siftly_ingestor': "SELECT MAX(updated_at) FROM twitter_bot.siftly_events",
        }
        for service, query in service_queries.items():
            row = fetch_one(cur, query)
            dashboard['service_last_success'][service] = row[0].isoformat() if row and row[0] else None

    appendix_path = Path(__file__).resolve().parents[2] / 'config' / 'system_prompt_appendix.txt'
    try:
        dashboard['voice_appendix'] = appendix_path.read_text().strip()
    except Exception:
        dashboard['voice_appendix'] = ''

    try:
        pm2 = subprocess.run(['pm2', 'jlist'], capture_output=True, text=True, check=True)
        dashboard['pm2'] = [
            {
                'name': item.get('name'),
                'status': item.get('pm2_env', {}).get('status'),
                'restart_count': item.get('pm2_env', {}).get('restart_time'),
                'memory_bytes': item.get('monit', {}).get('memory'),
            }
            for item in json.loads(pm2.stdout)
        ]
    except Exception:
        dashboard['pm2'] = []

    return dashboard


def main() -> int:
    parser = argparse.ArgumentParser(description='Show a small operational dashboard for the CS2 bot')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    dashboard = build_dashboard(conn)
    conn.close()

    if args.json:
        print(json.dumps(dashboard, indent=2))
        return 0

    print('queue_depth', dashboard['queue_depth'])
    print('ocr', dashboard['ocr'])
    print('media_attach', dashboard['media_attach'])
    print('approval', dashboard['approval'])
    print('bandit', dashboard['bandit'])
    print('service_last_success', dashboard['service_last_success'])
    print('voice_appendix', dashboard['voice_appendix'])
    print('generator_scorecards')
    if dashboard['generator_scorecards']:
        for row in dashboard['generator_scorecards']:
            print(
                f"  {row['avg_er']:.4f} | {row['tweet_count']:>3} | "
                f"{row['strategy']:<22} | {row['template_key']}"
            )
    else:
        print('  no template-keyed posted tweets yet')
    print('media_experiments')
    if dashboard['media_experiments']:
        for row in dashboard['media_experiments']:
            print(
                f"  {row['avg_er']:.4f} | {row['tweet_count']:>3} | "
                f"{row['card_type']:<20} | {row['variant']}"
            )
    else:
        print('  no media experiment rows yet')
    if dashboard['pm2']:
        print('pm2')
        for row in dashboard['pm2']:
            print(f"  {row['name']:<20} {row['status']:<10} restarts={row['restart_count']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())