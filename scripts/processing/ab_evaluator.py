#!/usr/bin/env python3
"""
A/B Test Evaluator — runs weekly (or on demand) to compare model performance
and auto-adjust pool weights based on engagement data.

Scoring formula:
  score = (likes * 3) + (retweets * 5) + (bookmarks * 2) + (quotes * 4) + (impressions * 0.001)
  normalized per tweet, so models with fewer tweets aren't penalized.

Minimum sample size: 5 tweets with engagement data before a model is scored.

Output:
  - Logs a leaderboard to stdout
  - Writes updated AB_POOL_AUTO and AB_POOL_PREMIUM to /dev/shm/.env
  - The next tweet_scheduler restart picks up new weights automatically

PM2 cron: runs every 7 days (weekly)
Can also be run manually: python3 processing/ab_evaluator.py
"""

import logging
import os
import re
import sys
from datetime import datetime, timezone

import psycopg2
from dotenv import load_dotenv

load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

DB_URL = os.getenv('DATABASE_URL', 'postgresql://postgres:izGsmCbbtxIXxFumxgCAoXmNaTdFFzTV@crossover.proxy.rlwy.net:55156/railway')
ENV_PATH = '/dev/shm/.env'
MIN_SAMPLE = 5          # minimum tweets with engagement before scoring
EVAL_DAYS = 7           # look back window
BASELINE_WEIGHT = 2     # minimum weight any model gets (never drops to 0)
MAX_WEIGHT = 5           # maximum weight cap


def get_db():
    return psycopg2.connect(DB_URL)


def fetch_model_stats(conn, days: int = EVAL_DAYS):
    """Fetch engagement stats per generation_model for tweets posted in the last N days."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 
                generation_model,
                COUNT(*) as tweet_count,
                COUNT(*) FILTER (WHERE impressions IS NOT NULL AND impressions > 0) as with_engagement,
                COALESCE(AVG(likes) FILTER (WHERE impressions > 0), 0) as avg_likes,
                COALESCE(AVG(retweets) FILTER (WHERE impressions > 0), 0) as avg_retweets,
                COALESCE(AVG(bookmarks) FILTER (WHERE impressions > 0), 0) as avg_bookmarks,
                COALESCE(AVG(quotes) FILTER (WHERE impressions > 0), 0) as avg_quotes,
                COALESCE(AVG(impressions) FILTER (WHERE impressions > 0), 0) as avg_impressions,
                COALESCE(AVG(engagement_rate) FILTER (WHERE impressions > 0), 0) as avg_er
            FROM twitter_bot.tweets_v2
            WHERE status = 'posted'
              AND created_at > NOW() - INTERVAL '%s days'
              AND generation_model IS NOT NULL
              AND generation_model NOT IN ('unknown', 'prediction_template')
            GROUP BY generation_model
            ORDER BY avg_likes DESC
        """, (days,))
        
        columns = ['model', 'total', 'with_engagement', 'avg_likes', 'avg_retweets',
                    'avg_bookmarks', 'avg_quotes', 'avg_impressions', 'avg_er']
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def score_model(stats: dict) -> float:
    """Compute a weighted engagement score for a model."""
    return (
        stats['avg_likes'] * 3 +
        stats['avg_retweets'] * 5 +
        stats['avg_bookmarks'] * 2 +
        stats['avg_quotes'] * 4 +
        stats['avg_impressions'] * 0.01
    )


def compute_weights(model_stats: list, pool_models: set) -> dict:
    """
    Given engagement stats and the set of models in a pool,
    return a {model: weight} dict with auto-tuned weights.
    
    Models with insufficient data keep BASELINE_WEIGHT.
    Models with data get weight proportional to their score.
    """
    scored = {}
    unscored = set()
    
    stats_by_model = {s['model']: s for s in model_stats}
    
    for model in pool_models:
        s = stats_by_model.get(model)
        if s and s['with_engagement'] >= MIN_SAMPLE:
            scored[model] = score_model(s)
        else:
            unscored.add(model)
    
    if not scored:
        # No data yet — keep all at baseline
        return {m: BASELINE_WEIGHT for m in pool_models}
    
    # Normalize scores to weights: best model gets MAX_WEIGHT, others proportionally
    max_score = max(scored.values())
    if max_score <= 0:
        return {m: BASELINE_WEIGHT for m in pool_models}
    
    weights = {}
    for model in pool_models:
        if model in scored:
            # Scale: score/max_score * MAX_WEIGHT, but at least BASELINE_WEIGHT
            raw = (scored[model] / max_score) * MAX_WEIGHT
            weights[model] = max(BASELINE_WEIGHT, round(raw))
        else:
            weights[model] = BASELINE_WEIGHT
    
    return weights


def update_env_pool(tier: str, weights: dict):
    """Write AB_POOL_<TIER>=model1:w1,model2:w2 into /dev/shm/.env"""
    env_key = f'AB_POOL_{tier.upper()}'
    pool_val = ','.join(f'{m}:{w}' for m, w in weights.items())
    
    # Read existing env
    with open(ENV_PATH, 'r') as f:
        content = f.read()
    
    # Replace or append
    pattern = rf'^{re.escape(env_key)}=.*$'
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, f'{env_key}={pool_val}', content, flags=re.MULTILINE)
    else:
        content = content.rstrip('\n') + f'\n{env_key}={pool_val}\n'
    
    with open(ENV_PATH, 'w') as f:
        f.write(content)
    
    logger.info(f"📝 Updated {env_key}={pool_val}")


def run_evaluation():
    logger.info(f"🧪 A/B Evaluator — checking last {EVAL_DAYS} days")
    
    conn = get_db()
    stats = fetch_model_stats(conn, EVAL_DAYS)
    conn.close()
    
    if not stats:
        logger.info("📭 No model data yet — too early to evaluate. Check back in a few days.")
        return
    
    # Print leaderboard
    logger.info("=" * 80)
    logger.info(f"{'MODEL':45s} {'TWEETS':>6s} {'w/ENG':>5s} {'LIKES':>6s} {'RT':>5s} {'BM':>4s} {'IMP':>7s} {'SCORE':>7s}")
    logger.info("-" * 80)
    
    for s in stats:
        sc = score_model(s)
        logger.info(
            f"{s['model']:45s} {s['total']:>6d} {s['with_engagement']:>5d} "
            f"{s['avg_likes']:>6.1f} {s['avg_retweets']:>5.1f} {s['avg_bookmarks']:>4.1f} "
            f"{s['avg_impressions']:>7.0f} {sc:>7.2f}"
        )
    
    logger.info("=" * 80)
    
    # Current pools (from openrouter_client defaults)
    auto_models = {
        os.getenv('LLM_TIER_AUTO', 'free/deepseek-v3.2'),
        'z-ai/glm-5.1',
        'minimax/minimax-m2.7',
        'xiaomi/mimo-v2-pro',
    }
    premium_models = {
        'anthropic/claude-sonnet-4.6',
        'z-ai/glm-5.1',
        'minimax/minimax-m2.7',
        'xiaomi/mimo-v2-pro',
    }
    
    # Check if we have enough data to auto-tune
    models_with_data = {s['model'] for s in stats if s['with_engagement'] >= MIN_SAMPLE}
    auto_have_data = auto_models & models_with_data
    premium_have_data = premium_models & models_with_data
    
    if len(auto_have_data) >= 2:
        auto_weights = compute_weights(stats, auto_models)
        logger.info(f"🎯 Auto tier weights: {auto_weights}")
        update_env_pool('auto', auto_weights)
    else:
        logger.info(f"⏳ Auto tier: only {len(auto_have_data)} models have enough data (need 2+). Keeping current weights.")
    
    if len(premium_have_data) >= 2:
        premium_weights = compute_weights(stats, premium_models)
        logger.info(f"🎯 Premium tier weights: {premium_weights}")
        update_env_pool('premium', premium_weights)
    else:
        logger.info(f"⏳ Premium tier: only {len(premium_have_data)} models have enough data (need 2+). Keeping current weights.")
    
    # Find the winner
    scoreable = [s for s in stats if s['with_engagement'] >= MIN_SAMPLE]
    if scoreable:
        best = max(scoreable, key=score_model)
        worst = min(scoreable, key=score_model)
        logger.info(f"🏆 Best model: {best['model']} (score={score_model(best):.2f}, {best['avg_likes']:.1f} avg likes)")
        logger.info(f"💤 Worst model: {worst['model']} (score={score_model(worst):.2f}, {worst['avg_likes']:.1f} avg likes)")
    else:
        logger.info("📊 Not enough engagement data yet to pick a winner. Keep running!")
    
    logger.info("✅ Evaluation complete. Restart tweet_scheduler to apply new weights.")


if __name__ == '__main__':
    run_evaluation()
