#!/usr/bin/env python3
"""Runtime-safe schema bootstrapping for incremental bot upgrades."""

import logging

logger = logging.getLogger(__name__)


def ensure_runtime_schema_extensions(conn):
    # Use a short lock_timeout so DDL never cascades into a full pipeline deadlock.
    # If we can't acquire the lock in 5s, the column/index already exists anyway.
    with conn.cursor() as cur:
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
    conn.commit()

    # DDL on tweets_v2 needs AccessExclusive — use short lock_timeout
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
                CREATE INDEX IF NOT EXISTS idx_tweets_account_bucket
                ON twitter_bot.tweets_v2(account_bucket)
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
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"⚠️  DDL lock_timeout on tweets_v2 (column likely exists): {e}")