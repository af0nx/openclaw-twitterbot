#!/usr/bin/env python3
"""
DB Utilities - Twitter Bot Pipeline V2
Shared PostgreSQL reconnection logic for all services.
Wraps psycopg2 connections with auto-reconnect on stale/broken connections.
"""

import logging
import os
import psycopg2

from dotenv import load_dotenv
load_dotenv('/dev/shm/.env')

logger = logging.getLogger(__name__)


def reset_connection_pools():
    """Compatibility hook for the pooled root helper; script helper is unpooled."""
    return None


def ensure_db_connection(conn, database_url=None, autocommit=False):
    """
    Check if a psycopg2 connection is alive, reconnect if not.

    Args:
        conn: Existing psycopg2 connection (or None)
        database_url: Optional DSN override. Supports legacy positional caller.
        autocommit: Whether to set autocommit on the connection

    Returns:
        A live psycopg2 connection (may be the same object or a fresh one)
    """
    if isinstance(database_url, bool) and autocommit is False:
        autocommit = database_url
        database_url = None

    if conn is not None:
        try:
            # Rollback any stale implicit transaction before health check
            # so CURRENT_DATE/NOW() reflect real time, not transaction start
            try:
                conn.rollback()
            except Exception:
                pass
            # Lightweight check — runs a no-op query
            conn.isolation_level  # triggers OperationalError if closed
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            # End the implicit transaction so the connection is truly idle
            # (not idle-in-transaction). Without this, idle_in_transaction_session_timeout
            # kills the session after 5 min, defeating keepalives.
            conn.rollback()
            conn.autocommit = autocommit
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError, psycopg2.DatabaseError):
            logger.warning("⚠️  DB connection stale — reconnecting…")
            try:
                conn.close()
            except Exception:
                pass

    # Create fresh connection with TCP keepalives enabled.
    # Railway's proxy drops idle TCP connections after ~60-300s.
    # Without client-side keepalives, psycopg2 only discovers the dead
    # connection on the next query → "stale" reconnect every single poll cycle.
    # keepalives_idle=30 sends the first heartbeat after 30s of idle,
    # then every 10s, failing after 5 missed probes (~80s total).
    new_conn = psycopg2.connect(
        database_url or os.getenv('DATABASE_URL'),
        connect_timeout=30,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
        options='-c statement_timeout=120000 -c idle_in_transaction_session_timeout=300000 -c search_path=twitter_bot,public',
    )
    new_conn.autocommit = autocommit
    logger.info("✅ Reconnected to PostgreSQL")
    return new_conn
