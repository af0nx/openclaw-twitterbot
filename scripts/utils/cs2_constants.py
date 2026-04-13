#!/usr/bin/env python3
"""
CS2 Constants - Twitter Bot Pipeline V2
Centralised keyword sets used across ingestion, scheduling, and fact-checking.
Import from here instead of copy-pasting these sets into every file.
"""
import re

# ── CS2 relevance keywords (broadest set) ────────────────────────────
CS2_KEYWORDS = {
    'cs2', 'cs:go', 'csgo', 'counter-strike', 'counterstrike',
    # Teams
    'navi', 'natus vincere', 'vitality', 'faze', 'g2', 'liquid', 'astralis',
    'mouz', 'mousesports', 'heroic', 'fnatic', 'spirit', 'furia', 'ence',
    'cloud9', 'big', 'eternal fire', 'complexity', 'imperial', 'monte',
    'saw', 'apeks', 'gamerlegion', 'sinners', '3dmax', 'pain',
    'the mongolz', 'lynn vision', 'virtus.pro',
    # Events
    'major', 'blast', 'esl', 'iem', 'faceit', 'hltv', 'pgl',
    'esl pro league', 'blast premier', 'betboom', 'perfect world',
    'thunderpick', 'cct', 'roobet cup',
    # Maps
    'awp', 'deagle', 'nuke', 'mirage', 'inferno', 'ancient', 'anubis',
    'dust2', 'vertigo', 'overpass', 'train',
    # Roles / terms
    'roster', 'igl', 'awper', 'entry', 'lurker', 'rifler',
    'ace', 'clutch', 'eco', 'force buy', 'map pick', 'veto',
    # Players
    'zywoo', 's1mple', 'niko', 'device', 'm0nesy', 'donk',
    # Economy
    'cs2 skin', 'cs2 sticker', 'capsule', 'knife skin', 'trade up',
    'esport', 'esports',
}

# Keywords that are common English words — require word-boundary matching
# to avoid false positives like "fence" matching "ence", etc.
# Only includes words that are CS2-specific enough to be useful with word boundaries.
_AMBIGUOUS_KEYWORDS = {
    'ence', 'niko', 'g2', 'spirit', 'faze',
}

# Keywords too generic even with word boundaries — only useful in team/event metadata,
# NOT for broad article filtering. Kept in CS2_KEYWORDS for reference but excluded from filtering.
_TOO_GENERIC_KEYWORDS = {
    'big', 'saw', 'pain', 'monte', 'imperial',  # team names = common words
    'heroic', 'ancient', 'train', 'nuke',        # map/team = common words
    'ace', 'clutch', 'eco', 'entry',              # FPS terms = common words
    'major', 'blast', 'liquid', 'device',         # event/team/player = common words
    'roster', 'veto', 'rain',                     # CS terms = common words
}

# Pre-compiled regex for ambiguous keywords (word-boundary match)
_AMBIGUOUS_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(kw) for kw in _AMBIGUOUS_KEYWORDS) + r')\b',
    re.IGNORECASE
)

# Safe keywords (non-ambiguous, unique enough for substring match)
_SAFE_KEYWORDS = CS2_KEYWORDS - _AMBIGUOUS_KEYWORDS - _TOO_GENERIC_KEYWORDS

# ── Non-CS2 keywords for negative filtering ──────────────────────────
NON_CS2_KEYWORDS = {
    'valorant', 'vct ', 'champions tour', 'overwatch', 'owcs',
    'dota 2', 'dota2', 'league of legends', ' lol ', ' lck', ' lpl',
    'call of duty', 'fortnite', 'apex legends', 'rocket league',
    'first stand 2026', 'worlds 2026',
}

# ── Known teams (canonical lowercase names) ──────────────────────────
KNOWN_CS2_TEAMS = {
    'navi', 'natus vincere', 'vitality', 'faze', 'faze clan',
    'g2', 'g2 esports', 'liquid', 'team liquid',
    'mouz', 'mousesports', 'heroic', 'spirit', 'team spirit',
    'fnatic', 'furia', 'furia esports', 'astralis',
    'complexity', 'eternal fire', 'virtus.pro',
    'cloud9', 'monte', 'imperial', '3dmax', 'sinners',
    'big', 'apeks', 'gamerlegion', 'saw', 'pain',
    'the mongolz', 'lynn vision', 'ence', 'mibr',
}

# ── Known players ────────────────────────────────────────────────────
KNOWN_PLAYERS = {
    'zywoo', 's1mple', 'niko', 'device', 'm0nesy', 'donk',
    'frozen', 'rain', 'karrigan', 'twistzz', 'elige', 'yekindar',
    'ropz', 'broky', 'electronic', 'bit', 'jl', 'spinx',
    'brollan', 'siuhy', 'torzsi', 'jimpphat', 'art', 'kscerato',
    'magisk', 'dupreeh', 'gla1ve', 'stavn', 'blamef',
    'ax1le', 'hobbit', 'perfecto', 'degster', 'xantares',
}

# ── Known operators / betting brands ─────────────────────────────────
KNOWN_OPERATORS = {
    'stake', 'draftkings', 'fanduel', 'betmgm', 'bet365',
    'thunderpick', 'csgoroll', 'pinnacle', 'bovada',
}


def _keyword_in_text(text_lower: str) -> bool:
    """Check if any CS2 keyword is in text, using word boundaries for ambiguous ones."""
    # Fast check: any safe (non-ambiguous) keyword matches as substring
    if any(kw in text_lower for kw in _SAFE_KEYWORDS):
        return True
    # Slower check: ambiguous keywords need word-boundary match
    return bool(_AMBIGUOUS_RE.search(text_lower))


def is_cs2_relevant(text: str) -> bool:
    """Quick check: does text contain CS2-related keywords?"""
    return _keyword_in_text(text.lower())


def is_other_game(text: str) -> bool:
    """Quick check: does text reference a non-CS2 game?"""
    text_lower = text.lower()
    return any(kw in text_lower for kw in NON_CS2_KEYWORDS)


# ── T1 players — high-profile players whose content is always T1 ──────
T1_PLAYERS = {
    'zywoo', 's1mple', 'niko', 'device', 'm0nesy', 'donk',
    'ropz', 'electronic', 'twistzz', 'elige', 'karrigan',
    'broky', 'frozen', 'brollan', 'siuhy', 'torzsi', 'jimpphat',
    'kscerato', 'fallen', 'yekindar', 'aleksib', 'magisk',
    'dupreeh', 'gla1ve', 'stavn', 'blamef', 'ax1le', 'hobbit',
    'xantares', 'wicadia', 'apex', 'flamez', 'mezii',
    'b1t', 'jame', 'perfecto', 'degster', 'hallzerk',
    'hunter', 'monesy', 'nexa', 'tabsen', 'syrson',
}

# Ambiguous player names that need word boundaries
_T1_AMBIGUOUS_PLAYERS = {'rain', 'art', 'jl', 'apex', 'bit', 'hunter'}
_T1_SAFE_PLAYERS = T1_PLAYERS - _T1_AMBIGUOUS_PLAYERS
_T1_AMBIGUOUS_PLAYERS_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(p) for p in _T1_AMBIGUOUS_PLAYERS) + r')\b',
    re.IGNORECASE
)


# ── T1 big-news topics — CS2 community-wide news that's always relevant ──
T1_BIG_NEWS = {
    'valve', 'vac', 'anticheat', 'anti-cheat', 'ban wave',
    'million', 'patch', 'prize pool', 'new case', 'operation',
    'source 2', 'overhaul',
}


# ── T1 filter — only top-tier CS2 content ─────────────────────────────
# T1 teams: HLTV top 15 caliber, the teams everyone cares about
T1_TEAMS = {
    'navi', 'natus vincere', 'na\'vi',
    'vitality', 'team vitality',
    'faze', 'faze clan',
    'g2', 'g2 esports',
    'spirit', 'team spirit',
    'mouz', 'mousesports',
    'liquid', 'team liquid',
    'heroic',
    'fnatic',
    'furia', 'furia esports',
    'astralis',
    'eternal fire',
    'virtus.pro',
    'cloud9',
    'complexity',
    'the mongolz',
    'ence',
}

# T1 events: premier-level tournaments only
T1_EVENTS = {
    'major', 'pgl major', 'pgl', 'blast premier', 'blast open', 'blast',
    'esl pro league', 'esl challenger', 'iem', 'iem katowice', 'iem cologne',
    'iem rio', 'iem dallas', 'iem chengdu',
    'perfect world', 'betboom', 'thunderpick',
    'cologne', 'katowice', 'rotterdam', 'bucharest', 'copenhagen',
    'gamers assembly',
}

# Categories that bypass the T1 filter (always allowed)
T1_BYPASS_CATEGORIES = {
    'cs2_update',          # Valve updates/patches — always relevant
    'roster_change',       # Roster changes on any T1 team
    'regulation',          # Bans, rule changes, org news
    'financial',           # Org acquisitions, prize pools
    'engagement_poll',     # Our engagement posts
    'engagement_conversation',
    'engagement_take',
    'engagement_recycle',
    'engagement_milestone',
    'vip_engagement',      # VIP tweet interactions
    'match_prediction',    # Prediction picks (pre-filtered by webhook)
}


# T1 teams that are also common English words — need word boundaries
_T1_AMBIGUOUS_TEAMS = {
    'ence', 'spirit', 'heroic', 'liquid', 'g2', 'faze',
    'vitality', 'big', 'complexity',
}
_T1_SAFE_TEAMS = T1_TEAMS - _T1_AMBIGUOUS_TEAMS
_T1_AMBIGUOUS_TEAMS_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(t) for t in _T1_AMBIGUOUS_TEAMS) + r')\b',
    re.IGNORECASE
)


def _has_t1_team(text: str) -> bool:
    """Check if text mentions a T1 team using word boundaries for ambiguous names."""
    if any(t in text for t in _T1_SAFE_TEAMS):
        return True
    return bool(_T1_AMBIGUOUS_TEAMS_RE.search(text))


def is_t1_content(headline: str, content: str, category: str, metadata: dict = None) -> bool:
    """Check if an event is T1-worthy (top-tier teams or events).
    
    Returns True if the event should be tweeted, False if T2/T3 noise.
    """
    # Bypass categories always pass
    if category in T1_BYPASS_CATEGORIES:
        return True
    
    text = (headline + ' ' + (content or '')).lower()
    
    # Check if any T1 team is mentioned (with word-boundary safety)
    has_t1_team = _has_t1_team(text)
    
    # Check if it's a T1 event
    has_t1_event = any(evt in text for evt in T1_EVENTS)
    
    # Check metadata for team names
    if metadata and not has_t1_team:
        team1 = (metadata.get('team1') or '').lower()
        team2 = (metadata.get('team2') or '').lower()
        event_name = (metadata.get('event') or '').lower()
        has_t1_team = any(t in team1 or t in team2 for t in T1_TEAMS)
        has_t1_event = has_t1_event or any(e in event_name for e in T1_EVENTS)
    
    # Check for T1 players (word-boundary safe)
    has_t1_player = any(p in text for p in _T1_SAFE_PLAYERS)
    if not has_t1_player:
        has_t1_player = bool(_T1_AMBIGUOUS_PLAYERS_RE.search(text))

    # Check for big CS2 news topics (valve updates, ban waves, etc.)
    has_big_news = any(t in text for t in T1_BIG_NEWS)

    # T1 team + any event = OK
    # T1 event + any team = OK
    # T1 player = OK (star player content is always newsworthy)
    # Big CS2 news = OK (e.g. Valve bans, game updates)
    # Neither = skip
    return has_t1_team or has_t1_event or has_t1_player or has_big_news
