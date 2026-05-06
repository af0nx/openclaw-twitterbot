#!/usr/bin/env python3
"""Helpers for routing tweets to the correct X account bucket."""

import os
from typing import Dict


ACCOUNT_BUCKETS = ('main', 'live', 'replies')


def select_account_bucket(
    pillar: int = None,
    pillar_name: str = '',
    reply_target_id: str = None,
    quote_tweet_id: str = None,
    explicit_bucket: str = None,
) -> str:
    if explicit_bucket in ACCOUNT_BUCKETS:
        return explicit_bucket

    pillar_name = (pillar_name or '').lower()
    if pillar_name == 'live_narration':
        return 'live'

    if reply_target_id or quote_tweet_id or pillar == 12:
        return 'replies'

    return 'main'


def get_bucket_daily_cap(bucket: str) -> int:
    bucket = bucket if bucket in ACCOUNT_BUCKETS else 'main'
    specific = os.getenv(f'X_{bucket.upper()}_DAILY_CAP')
    if specific:
        return int(specific)
    return int(os.getenv('DAILY_TWEET_CAP', '10'))


def get_account_credentials(bucket: str) -> Dict[str, str]:
    bucket = bucket if bucket in ACCOUNT_BUCKETS else 'main'
    if bucket == 'main':
        prefix = 'X_'
        fallback_prefix = 'X_'
    else:
        prefix = f'X_{bucket.upper()}_'
        fallback_prefix = 'X_'

    def value(name: str) -> str:
        return os.getenv(prefix + name) or os.getenv(fallback_prefix + name) or ''

    return {
        'api_key': value('API_KEY'),
        'api_secret': value('API_SECRET'),
        'access_token': value('ACCESS_TOKEN'),
        'access_secret': value('ACCESS_SECRET'),
        'bearer_token': value('BEARER_TOKEN'),
        'screen_name': os.getenv(f'X_{bucket.upper()}_SCREEN_NAME') or os.getenv('X_SCREEN_NAME') or bucket,
    }
