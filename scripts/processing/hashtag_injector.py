#!/usr/bin/env python3
"""
Hashtag Injector - Twitter Bot Pipeline V2 Growth Engine
Auto-injects relevant CS2 hashtags into tweets before posting.
Max 2-3 hashtags per tweet to avoid looking spammy.
"""

import re
import logging

logger = logging.getLogger(__name__)

# Always include on every tweet
ALWAYS_HASHTAGS = ['#CS2']

# Event-specific hashtags (matched against event metadata + content)
EVENT_HASHTAGS = {
    'iem': ['#IEM'],
    'iem katowice': ['#IEM', '#IEMKatowice'],
    'iem cologne': ['#IEM', '#IEMCologne'],
    'iem dallas': ['#IEM', '#IEMDallas'],
    'blast': ['#BLASTPremier'],
    'blast premier': ['#BLASTPremier'],
    'pgl': ['#PGL'],
    'pgl major': ['#PGL', '#CS2Major'],
    'esl': ['#ESL'],
    'esl pro league': ['#ESL', '#ESLProLeague'],
    'major': ['#CS2Major'],
}

# Category-specific hashtags
CATEGORY_HASHTAGS = {
    'roster_change': ['#CS2Roster'],
    'match_result': ['#CS2Esports'],
    'cs2_update': ['#CS2Update'],
    'regulation': ['#CS2Esports'],
    'financial': ['#Esports'],
}

# Max total hashtags per tweet (including any already in the text)
MAX_HASHTAGS = 3


def inject_hashtags(tweet_text: str, category: str = None, event: dict = None) -> str:
    """
    Inject relevant hashtags into a tweet.
    
    - Respects 280 char limit
    - Won't duplicate hashtags already in the text
    - Max 3 hashtags total
    - Appends to end of tweet on new line
    
    Args:
        tweet_text: The tweet content
        category: Event category (roster_change, match_result, etc.)
        event: Full event dict with headline/content/metadata
        
    Returns:
        Tweet text with hashtags appended
    """
    if not tweet_text:
        return tweet_text
    
    # Count existing hashtags in the tweet
    existing_tags = set(tag.lower() for tag in re.findall(r'#\w+', tweet_text))
    existing_count = len(existing_tags)
    
    if existing_count >= MAX_HASHTAGS:
        return tweet_text
    
    budget = MAX_HASHTAGS - existing_count
    tags_to_add = []
    
    # 1. Always add #CS2 if not present
    if '#cs2' not in existing_tags and budget > 0:
        tags_to_add.append('#CS2')
        budget -= 1
    
    # 2. Add event-specific hashtags
    if budget > 0 and event:
        text_lower = (
            (event.get('headline') or '') + ' ' +
            (event.get('content') or '')
        ).lower()
        
        # Check longest matches first (more specific)
        for pattern in sorted(EVENT_HASHTAGS.keys(), key=len, reverse=True):
            if pattern in text_lower:
                for tag in EVENT_HASHTAGS[pattern]:
                    if tag.lower() not in existing_tags and tag not in tags_to_add and budget > 0:
                        tags_to_add.append(tag)
                        budget -= 1
                break  # Only match the most specific event
    
    # 3. Add category-specific hashtags
    if budget > 0 and category and category in CATEGORY_HASHTAGS:
        for tag in CATEGORY_HASHTAGS[category]:
            if tag.lower() not in existing_tags and tag not in tags_to_add and budget > 0:
                tags_to_add.append(tag)
                budget -= 1
    
    if not tags_to_add:
        return tweet_text
    
    # Build the final tweet with hashtags
    hashtag_suffix = ' '.join(tags_to_add)
    
    # Check if adding hashtags would exceed 280 chars
    # Try appending on same line first, then trim tags if needed
    candidate = f"{tweet_text.rstrip()}\n\n{hashtag_suffix}"
    
    while len(candidate) > 280 and tags_to_add:
        tags_to_add.pop()
        if tags_to_add:
            hashtag_suffix = ' '.join(tags_to_add)
            candidate = f"{tweet_text.rstrip()}\n\n{hashtag_suffix}"
        else:
            return tweet_text
    
    return candidate


def inject_hashtags_thread(thread_tweets: list, category: str = None, event: dict = None) -> list:
    """
    Inject hashtags into the FIRST tweet of a thread only.
    """
    if not thread_tweets:
        return thread_tweets
    
    result = list(thread_tweets)
    if isinstance(result[0], dict):
        result[0] = dict(result[0])
        result[0]['text'] = inject_hashtags(result[0]['text'], category, event)
    elif isinstance(result[0], str):
        result[0] = inject_hashtags(result[0], category, event)
    
    return result
