"""Lightweight tweet text validation shared by generators and posters."""

from __future__ import annotations

import re
from typing import Optional


LEAK_MARKERS = [
    'we need to', 'we need a', 'we must', 'we should',
    'let\'s craft', 'let\'s write', 'let\'s create', 'let\'s generate',
    'craft a tweet', 'write a tweet', 'generate a tweet', 'create a tweet',
    'generate one tweet', 'generate a', 'write a',
    'tweet 1:', 'tweet 2:', 'tweet 3:',
    'max 280', 'max 270', '280 char', '270 char',
    'separated by', 'one idea per tweet', 'provide result',
    'no explanation', 'just the tweet', 'output only',
    'here is the tweet', 'here\'s the tweet', 'here\'s a tweet',
    'your critique', 'your task', 'your job',
    'pillar context', 'event data', 'headline:',
    'format:', 'rules:', 'ensure no',
    'b2 english', 'simple words', 'short sentences',
    'need to identify', 'must be 60', 'mention that', 'add maybe',
    'the user wants', 'the user asked', 'user wants', 'user asked',
    'rewrite the original', 'original tweet:', 'original poll question',
    'make this poll', 'good angles:', 'must reference', 'must be <',
    'in the style of', 'study the format', 'study the length',
    'respond with', 'return only', 'nothing else',
    'as an ai', 'i cannot', "i can't", "i'm sorry",
    'must identify', 'need to determine', 'let me think',
    'i should', 'first,', 'step 1', 'step 2',
    'my task', 'the prompt', 'the headline', 'the event:',
    'the content:', 'the category', 'the source',
    'i\'ll write', 'i\'ll craft', 'i\'ll generate',
    'i will write', 'i will craft', 'i will generate',
    'must be under', 'keep it under', 'stay under',
    'analyzing', 'identify the', 'determine the',
]

REASONING_PATTERNS = re.compile(
    r'(?:'
    r'^must\s+(?:identify|determine|find|check|verify|include|mention)'
    r'|^(?:need|trying|going) to (?:identify|determine|find|figure)'
    r'|^(?:first|okay|alright|so),?\s+(?:i|we|let)'
    r'|(?:the event|the headline|event data|pillar \d+)\s*[:.]'
    r')',
    re.IGNORECASE | re.MULTILINE,
)

META_RESPONSE_PATTERNS = [
    re.compile(r"it seems like there(?:'s| is) a missing element in your request", re.IGNORECASE),
    re.compile(r'\bplease provide\b.{0,80}\b(?:tweet|draft)\s+text\b', re.IGNORECASE | re.DOTALL),
    re.compile(r'\bdraft tweet text\b', re.IGNORECASE),
    re.compile(r"\byou(?:'d| would) like me to review\b", re.IGNORECASE),
    re.compile(r'\brespond with only\b', re.IGNORECASE),
    re.compile(r'\bone short reason to reject\b', re.IGNORECASE),
    re.compile(r'\byour request\b', re.IGNORECASE),
    re.compile(r'\b(?:editor feedback|realitychecker)\b', re.IGNORECASE),
]


def normalize_generated_text(text: Optional[str]) -> str:
    if text is None:
        return ''

    cleaned = str(text).strip()
    for preamble in (
        'Tweet:', 'Here\'s', 'Sure,', 'Here is', 'Output:', 'Draft:',
        'Revised:', 'Revision:'
    ):
        if cleaned.lower().startswith(preamble.lower()):
            cleaned = cleaned[len(preamble):].lstrip(' :')

    cleaned = re.sub(
        r'^(okay,?\s*|so,?\s*|alright,?\s*|let me|i\'ll|i will|we need to|we should|let\'s)\s*.{0,60}(tweet|post|write|craft|generate)\b[^.]*\.\s*',
        '',
        cleaned,
        count=1,
        flags=re.IGNORECASE,
    ).strip()

    if ((cleaned.startswith('"') and cleaned.endswith('"'))
            or (cleaned.startswith("'") and cleaned.endswith("'"))):
        cleaned = cleaned[1:-1]

    return cleaned.strip()


def tweet_quality_issue(text: Optional[str]) -> Optional[str]:
    cleaned = normalize_generated_text(text)
    if not cleaned:
        return 'empty tweet'

    if len(cleaned) > 280:
        return f'tweet exceeds 280 chars ({len(cleaned)})'

    lower = cleaned.lower()
    if any(marker in lower for marker in LEAK_MARKERS):
        return 'prompt or reasoning leakage'
    if REASONING_PATTERNS.search(cleaned):
        return 'reasoning leakage'
    if cleaned.upper() in ('APPROVED', 'REJECTED'):
        return 'review verdict leaked'
    if cleaned.upper().startswith('APPROVED '):
        return 'review verdict leaked'
    if any(pattern.search(cleaned) for pattern in META_RESPONSE_PATTERNS):
        return 'meta response leaked'
    if re.search(r'\n\s*(?:[-*]|\d+\.)\s+', cleaned):
        return 'list formatting leaked'
    if re.search(r'https?://', cleaned, re.IGNORECASE):
        return 'raw URL in tweet'
    if re.search(r'(^|\s)#\w+', cleaned):
        return 'hashtag in tweet'
    if cleaned.count('"') >= 4 and len(cleaned) > 180:
        return 'quote-heavy generated text'
    return None


def is_invalid_tweet_candidate(text: Optional[str]) -> bool:
    return tweet_quality_issue(text) is not None
