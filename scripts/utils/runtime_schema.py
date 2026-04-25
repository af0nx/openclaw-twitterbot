#!/usr/bin/env python3
"""Runtime-safe schema bootstrapping for incremental bot upgrades."""

import logging

from psycopg2 import sql

logger = logging.getLogger(__name__)

_RUNTIME_SCHEMA_LOCK_KEYS = (0x4F434C41, 0x52545343)

_ROLLUP_VIEWS = (
    'mv_generator_template_30d',
    'mv_generator_daily_metrics',
    'mv_media_experiment_30d',
)


def _acquire_runtime_schema_lock(conn):
    try:
        conn.rollback()
    except Exception:
        pass

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s, %s)", _RUNTIME_SCHEMA_LOCK_KEYS)
    conn.commit()


def _release_runtime_schema_lock(conn):
    try:
        conn.rollback()
    except Exception:
        pass

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s, %s)", _RUNTIME_SCHEMA_LOCK_KEYS)
        conn.commit()
    except Exception:
        conn.rollback()


def ensure_runtime_schema_extensions(conn):
    _acquire_runtime_schema_lock(conn)
    try:
        _ensure_runtime_schema_extensions_locked(conn)
    finally:
        _release_runtime_schema_lock(conn)


def _ensure_runtime_schema_extensions_locked(conn):
    # Use a short lock_timeout so DDL never cascades into a full pipeline deadlock.
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS twitter_bot.account_quotas (
                date DATE NOT NULL DEFAULT CURRENT_DATE,
                account_bucket TEXT NOT NULL,
                writes_executed INT DEFAULT 0,
                writes_reserved INT DEFAULT 0,
                hard_capped BOOLEAN DEFAULT FALSE,
                last_post_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (date, account_bucket)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS twitter_bot.engagement_tracking (
                id BIGSERIAL PRIMARY KEY,
                tweet_v2_id UUID REFERENCES twitter_bot.tweets_v2(id) ON DELETE CASCADE,
                twitter_tweet_id TEXT,
                impressions INT DEFAULT 0,
                likes INT DEFAULT 0,
                replies INT DEFAULT 0,
                retweets INT DEFAULT 0,
                quotes INT DEFAULT 0,
                bookmarks INT DEFAULT 0,
                engagement_rate FLOAT DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            ALTER TABLE twitter_bot.engagement_tracking
            ADD COLUMN IF NOT EXISTS twitter_tweet_id TEXT
            """
        )
        cur.execute(
            """
            ALTER TABLE twitter_bot.engagement_tracking
            ADD COLUMN IF NOT EXISTS impressions INT DEFAULT 0
            """
        )
        cur.execute(
            """
            ALTER TABLE twitter_bot.engagement_tracking
            ADD COLUMN IF NOT EXISTS likes INT DEFAULT 0
            """
        )
        cur.execute(
            """
            ALTER TABLE twitter_bot.engagement_tracking
            ADD COLUMN IF NOT EXISTS replies INT DEFAULT 0
            """
        )
        cur.execute(
            """
            ALTER TABLE twitter_bot.engagement_tracking
            ADD COLUMN IF NOT EXISTS retweets INT DEFAULT 0
            """
        )
        cur.execute(
            """
            ALTER TABLE twitter_bot.engagement_tracking
            ADD COLUMN IF NOT EXISTS quotes INT DEFAULT 0
            """
        )
        cur.execute(
            """
            ALTER TABLE twitter_bot.engagement_tracking
            ADD COLUMN IF NOT EXISTS bookmarks INT DEFAULT 0
            """
        )
        cur.execute(
            """
            ALTER TABLE twitter_bot.engagement_tracking
            ADD COLUMN IF NOT EXISTS engagement_rate FLOAT DEFAULT 0
            """
        )
        cur.execute(
            """
            ALTER TABLE twitter_bot.engagement_tracking
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_engagement_tracking_tweet_v2_id
            ON twitter_bot.engagement_tracking(tweet_v2_id, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_engagement_tracking_twitter_tweet_id
            ON twitter_bot.engagement_tracking(twitter_tweet_id, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS twitter_bot.team_ratings (
                team_key TEXT PRIMARY KEY,
                team_name TEXT NOT NULL,
                rating FLOAT NOT NULL,
                matches_played INT NOT NULL DEFAULT 0,
                wins INT NOT NULL DEFAULT 0,
                losses INT NOT NULL DEFAULT 0,
                recent_form TEXT,
                recent_delta FLOAT DEFAULT 0,
                confidence FLOAT DEFAULT 0,
                rating_version TEXT NOT NULL DEFAULT 'elo_v1',
                last_match_at TIMESTAMPTZ,
                last_rebuilt_at TIMESTAMPTZ DEFAULT NOW(),
                metadata JSONB DEFAULT '{}'::jsonb
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_team_ratings_rating_desc
            ON twitter_bot.team_ratings(rating DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_team_ratings_last_match_at
            ON twitter_bot.team_ratings(last_match_at DESC)
            """
        )
    conn.commit()

    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute(
                """
                ALTER TABLE twitter_bot.tweets_v2
                ADD COLUMN IF NOT EXISTS account_bucket TEXT
                """
            )
            cur.execute(
                """
                ALTER TABLE twitter_bot.tweets_v2
                ADD COLUMN IF NOT EXISTS media_preview_path TEXT
                """
            )
            cur.execute(
                """
                ALTER TABLE twitter_bot.tweets_v2
                ADD COLUMN IF NOT EXISTS auto_approved BOOLEAN DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE twitter_bot.tweets_v2
                ADD COLUMN IF NOT EXISTS scheduled_post_at TIMESTAMPTZ
                """
            )
            cur.execute(
                """
                ALTER TABLE twitter_bot.tweets_v2
                ADD COLUMN IF NOT EXISTS is_thread BOOLEAN DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE twitter_bot.tweets_v2
                ADD COLUMN IF NOT EXISTS thread_tweets JSONB
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tweets_account_bucket
                ON twitter_bot.tweets_v2(account_bucket)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tweets_status_scheduled_post_at
                ON twitter_bot.tweets_v2(status, scheduled_post_at)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tweets_posted_recent_lookup
                ON twitter_bot.tweets_v2(posted_at DESC)
                WHERE status = 'posted'
                  AND twitter_tweet_id IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tweets_vip_reply_targets_posted
                ON twitter_bot.tweets_v2(posted_at DESC)
                WHERE reply_target_id IS NOT NULL
                  AND pillar = 12
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tweets_engagement_targets_created
                ON twitter_bot.tweets_v2(created_at DESC)
                WHERE reply_target_id IS NOT NULL
                   OR quote_tweet_id IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_twitter_vip_source_url
                ON twitter_bot.events(source_url)
                WHERE source = 'twitter'
                  AND category = 'vip_engagement'
                  AND source_url IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_live_match_recent
                ON twitter_bot.events(created_at DESC)
                WHERE category IN ('match_result', 'match_preview')
                  AND metadata->>'team1' IS NOT NULL
                  AND metadata->>'team2' IS NOT NULL
                """
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"⚠️  DDL lock_timeout during runtime schema bootstrap: {e}")

    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute(
                """
                CREATE MATERIALIZED VIEW IF NOT EXISTS twitter_bot.mv_generator_template_30d AS
                SELECT
                    e.category,
                    COALESCE(e.metadata->>'strategy', 'unknown') AS strategy,
                    COALESCE(e.metadata->>'template_key', 'unknown') AS template_key,
                    COUNT(*)::INT AS tweet_count,
                    AVG(t.engagement_rate) AS avg_er,
                    AVG(t.impressions) AS avg_impressions,
                    SUM(t.likes)::INT AS total_likes,
                    SUM(t.replies)::INT AS total_replies
                FROM twitter_bot.tweets_v2 t
                JOIN twitter_bot.events e ON e.id = t.event_id
                WHERE t.status = 'posted'
                  AND t.posted_at > NOW() - INTERVAL '30 days'
                  AND t.engagement_rate IS NOT NULL
                  AND e.metadata IS NOT NULL
                  AND e.metadata ? 'template_key'
                GROUP BY 1, 2, 3
                WITH DATA
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_generator_template_30d_key
                ON twitter_bot.mv_generator_template_30d(category, strategy, template_key)
                """
            )
            cur.execute(
                """
                CREATE MATERIALIZED VIEW IF NOT EXISTS twitter_bot.mv_generator_daily_metrics AS
                SELECT
                    DATE(t.posted_at) AS day,
                    e.category,
                    COALESCE(e.metadata->>'strategy', 'unknown') AS strategy,
                    COUNT(*)::INT AS tweet_count,
                    AVG(t.engagement_rate) AS avg_er,
                    AVG(t.impressions) AS avg_impressions,
                    SUM(t.likes)::INT AS total_likes,
                    SUM(t.replies)::INT AS total_replies
                FROM twitter_bot.tweets_v2 t
                JOIN twitter_bot.events e ON e.id = t.event_id
                WHERE t.status = 'posted'
                  AND t.posted_at > NOW() - INTERVAL '30 days'
                GROUP BY 1, 2, 3
                WITH DATA
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_generator_daily_metrics_key
                ON twitter_bot.mv_generator_daily_metrics(day, category, strategy)
                """
            )
            cur.execute(
                """
                CREATE MATERIALIZED VIEW IF NOT EXISTS twitter_bot.mv_media_experiment_30d AS
                SELECT
                    e.category,
                    COALESCE(e.metadata->'media_experiment'->>'card_type', 'unknown') AS card_type,
                    COALESCE(e.metadata->'media_experiment'->>'variant', 'unknown') AS variant,
                    COUNT(*)::INT AS tweet_count,
                    AVG(t.engagement_rate) AS avg_er,
                    AVG(t.impressions) AS avg_impressions,
                    AVG(t.likes) AS avg_likes,
                    AVG(t.replies) AS avg_replies
                FROM twitter_bot.tweets_v2 t
                JOIN twitter_bot.events e ON e.id = t.event_id
                WHERE t.status = 'posted'
                  AND t.posted_at > NOW() - INTERVAL '30 days'
                  AND e.metadata IS NOT NULL
                  AND e.metadata ? 'media_experiment'
                GROUP BY 1, 2, 3
                WITH DATA
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_media_experiment_30d_key
                ON twitter_bot.mv_media_experiment_30d(category, card_type, variant)
                """
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"⚠️  Could not create runtime rollups: {e}")


def refresh_runtime_rollups(conn):
    """Refresh analytics rollups used by generators, media experiments, and ops."""
    for view_name in _ROLLUP_VIEWS:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("REFRESH MATERIALIZED VIEW CONCURRENTLY {}.{}")
                    .format(sql.Identifier('twitter_bot'), sql.Identifier(view_name))
                )
            conn.commit()
        except Exception:
            conn.rollback()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("REFRESH MATERIALIZED VIEW {}.{}")
                        .format(sql.Identifier('twitter_bot'), sql.Identifier(view_name))
                    )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning(f"⚠️  Could not refresh {view_name}: {e}")