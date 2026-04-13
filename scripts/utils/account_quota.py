#!/usr/bin/env python3
"""Per-account quota helpers for sharded X posting."""

import logging

logger = logging.getLogger(__name__)


def reserve_account_slot(conn, bucket: str, daily_cap: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO twitter_bot.account_quotas (date, account_bucket)
            VALUES (CURRENT_DATE, %s)
            ON CONFLICT (date, account_bucket) DO NOTHING
            """,
            (bucket,),
        )
        cur.execute(
            """
            SELECT writes_executed, writes_reserved, hard_capped
            FROM twitter_bot.account_quotas
            WHERE date = CURRENT_DATE AND account_bucket = %s
            FOR UPDATE
            """,
            (bucket,),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return False

        executed, reserved, hard_capped = row
        if hard_capped or (executed + reserved) >= daily_cap:
            conn.rollback()
            return False

        cur.execute(
            """
            UPDATE twitter_bot.account_quotas
            SET writes_reserved = writes_reserved + 1,
                updated_at = NOW()
            WHERE date = CURRENT_DATE AND account_bucket = %s
            """,
            (bucket,),
        )
        conn.commit()
        return True


def free_account_slot(conn, bucket: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE twitter_bot.account_quotas
            SET writes_reserved = GREATEST(0, writes_reserved - 1),
                updated_at = NOW()
            WHERE date = CURRENT_DATE AND account_bucket = %s
            """,
            (bucket,),
        )
    conn.commit()


def increment_account_quota(conn, bucket: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO twitter_bot.account_quotas (date, account_bucket, writes_executed)
            VALUES (CURRENT_DATE, %s, 1)
            ON CONFLICT (date, account_bucket) DO UPDATE
            SET writes_executed = twitter_bot.account_quotas.writes_executed + 1,
                writes_reserved = GREATEST(0, twitter_bot.account_quotas.writes_reserved - 1),
                last_post_at = NOW(),
                updated_at = NOW()
            """,
            (bucket,),
        )
        cur.execute(
            """
            INSERT INTO twitter_bot.api_quotas (date, writes_executed)
            VALUES (CURRENT_DATE, 1)
            ON CONFLICT (date) DO UPDATE
            SET writes_executed = twitter_bot.api_quotas.writes_executed + 1,
                writes_reserved = GREATEST(0, twitter_bot.api_quotas.writes_reserved - 1),
                last_post_at = NOW(),
                updated_at = NOW()
            """
        )
    conn.commit()


def get_account_quota_snapshot(conn, bucket: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT writes_executed, writes_reserved, hard_capped
            FROM twitter_bot.account_quotas
            WHERE date = CURRENT_DATE AND account_bucket = %s
            """,
            (bucket,),
        )
        row = cur.fetchone()
    if not row:
        return {'writes_executed': 0, 'writes_reserved': 0, 'hard_capped': False}
    return {
        'writes_executed': row[0],
        'writes_reserved': row[1],
        'hard_capped': row[2],
    }