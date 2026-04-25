#!/usr/bin/env python3
"""
Post-Match Analyzer — Twitter Bot Pipeline V2
Evaluates T1 CS2 matches after they finish with deep analysis.
Cross-references our AI's pre-match predictions to build credibility.

Flow:
  1. HLTV monitor detects finished match → event lands in DB
  2. match_analyzer picks up match_result events with metadata
  3. Scrapes HLTV match page for detailed stats (maps, players, ratings)
  4. LLM generates precise analysis tweet or thread
  5. Cross-checks: did we tweet a prediction? → include accuracy callout
  6. Queues analysis tweet (auto-approve or HITL depending on confidence)

Runs inside tweet_scheduler pipeline — called when category == 'match_result'
"""

import logging
import re
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import os

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.openrouter_client import get_openrouter_client
from processing.team_rating_engine import canonicalize_team_name

# Load environment
load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# T1 events that deserve full analysis
T1_EVENTS = {
    'esl pro league', 'blast', 'iem', 'pgl', 'major',
    'esl challenger', 'betboom', 'perfect world',
    'thunderpick', 'cct', 'roobet cup',
}

# Top teams that always get analysis
T1_TEAMS = {
    'natus vincere', 'navi', 'vitality', 'faze', 'faze clan',
    'g2', 'g2 esports', 'liquid', 'team liquid',
    'mouz', 'mousesports', 'heroic', 'spirit', 'team spirit',
    'fnatic', 'furia', 'furia esports', 'astralis',
    'complexity', 'eternal fire', 'virtus.pro',
    'cloud9', 'monte', 'imperial', '3dmax', 'sinners',
    'big', 'apeks', 'gamerlegion', 'saw', 'pain',
    'the mongolz', 'lynn vision',
}


class MatchAnalyzer:
    """Deep post-match analysis for T1 CS2 matches"""

    def __init__(self):
        self.client = get_openrouter_client()
        self.db_conn = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='match_scrape')

    def connect_db(self, conn=None):
        """Use existing connection or create new one"""
        if conn:
            self.db_conn = conn
        else:
            self.db_conn = psycopg2.connect(os.getenv('DATABASE_URL'))

    def is_t1_match(self, event: Dict[str, Any]) -> bool:
        """Check if this match deserves full post-match analysis"""
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            return False

        event_name = (metadata.get('event_name') or metadata.get('event') or '').lower()
        team1 = (metadata.get('team1') or metadata.get('teams', [None, None])[0] or '').lower() if isinstance(metadata.get('teams'), list) else (metadata.get('team1') or '').lower()
        team2 = (metadata.get('team2') or (metadata.get('teams', [None, None])[1] if isinstance(metadata.get('teams'), list) else '') or '').lower()

        # Must be a T1 event
        is_t1_event = any(t1 in event_name for t1 in T1_EVENTS)
        # Or involve a T1 team
        has_t1_team = any(t in team1 or t in team2 for t in T1_TEAMS)

        return is_t1_event or has_t1_team

    def extract_match_data(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Extract structured match data from event metadata"""
        metadata = event.get('metadata') or {}
        headline = event.get('headline', '')
        content = event.get('content', '')

        # Try to extract from metadata first
        if isinstance(metadata.get('teams'), list) and len(metadata['teams']) >= 2:
            team1 = metadata['teams'][0]
            team2 = metadata['teams'][1]
        else:
            team1 = metadata.get('team1', 'Unknown')
            team2 = metadata.get('team2', 'Unknown')

        if isinstance(metadata.get('score'), str) and '-' in metadata['score']:
            parts = metadata['score'].split('-')
            score1, score2 = parts[0].strip(), parts[1].strip()
        else:
            score1 = str(metadata.get('score1', '?'))
            score2 = str(metadata.get('score2', '?'))

        winner = metadata.get('winner', '')
        event_name = metadata.get('event_name') or metadata.get('event', '')

        # Map data if available
        maps = metadata.get('maps', [])

        # Community vibe from HLTV comments
        community_vibe = metadata.get('community_vibe', {})
        memes = community_vibe.get('memes', []) if isinstance(community_vibe, dict) else []
        team_sentiment = community_vibe.get('team_sentiment', {}) if isinstance(community_vibe, dict) else {}
        top_comments = community_vibe.get('top_comments', []) if isinstance(community_vibe, dict) else []

        # Swarm confidence (MiroFish)
        swarm_confidence = metadata.get('swarm_confidence', None)

        return {
            'team1': team1,
            'team2': team2,
            'score1': score1,
            'score2': score2,
            'winner': winner,
            'event': event_name,
            'maps': maps,
            'headline': headline,
            'content': content,
            'memes': memes,
            'team_sentiment': team_sentiment,
            'top_comments': top_comments,
            'swarm_confidence': swarm_confidence,
            'hltv_match_id': metadata.get('hltv_match_id'),
            'match_context': metadata.get('match_context', {}),
            'rating_context': metadata.get('rating_context', {}),
            'metadata': metadata,
        }

    def find_our_predictions(self, team1: str, team2: str) -> List[Dict[str, Any]]:
        """Find any tweets we posted that predicted this match outcome"""
        if not self.db_conn:
            return []

        try:
            with self.db_conn.cursor() as cur:
                # Search for tweets mentioning both teams in the last 3 days
                t1_lower = team1.lower()
                t2_lower = team2.lower()

                cur.execute("""
                    SELECT id, content, twitter_tweet_id, posted_at
                    FROM twitter_bot.tweets_v2
                    WHERE status = 'posted'
                      AND posted_at > NOW() - INTERVAL '3 days'
                      AND (
                          LOWER(content) LIKE %s OR LOWER(content) LIKE %s
                      )
                    ORDER BY posted_at DESC
                    LIMIT 5
                """, (f'%{t1_lower}%', f'%{t2_lower}%'))

                predictions = []
                for row in cur.fetchall():
                    tweet_id, content, twitter_id, posted_at = row
                    # Check if it mentions both teams (actual prediction)
                    content_lower = content.lower()
                    if t1_lower in content_lower and t2_lower in content_lower:
                        predictions.append({
                            'id': str(tweet_id),
                            'content': content,
                            'twitter_tweet_id': twitter_id,
                            'posted_at': posted_at,
                        })

                return predictions

        except Exception as e:
            logger.warning(f"⚠️  Error searching predictions: {e}")
            return []

    def check_prediction_accuracy(self, prediction_content: str, winner: str, team1: str, team2: str) -> Optional[str]:
        """Use LLM to check if our prediction was correct"""
        try:
            result = self.client.generate(
                prompt=(
                    f'Our tweet: "{prediction_content}"\n\n'
                    f'Match result: {team1} vs {team2}, winner: {winner}\n\n'
                    f'Did our tweet predict a winner? If so, was the prediction correct?\n'
                    f'Reply ONLY with one of: CORRECT, WRONG, NO_PREDICTION'
                ),
                tier='eco',
                temperature=0.1,
                max_tokens=20
            )
            verdict = result['text'].strip().upper()
            if 'CORRECT' in verdict:
                return 'correct'
            elif 'WRONG' in verdict:
                return 'wrong'
            return None
        except Exception as e:
            logger.warning(f"⚠️  Prediction check failed: {e}")
            return None

    def generate_analysis(self, match_data: Dict[str, Any], prediction_info: Optional[Dict] = None) -> Dict[str, Any]:
        """Generate deep post-match analysis tweet using LLM"""

        # Build the analysis prompt
        prompt_parts = [
            "POST-MATCH ANALYSIS — Generate a sharp, high-energy analysis tweet.",
            "",
            f"MATCH: {match_data['team1']} {match_data['score1']}-{match_data['score2']} {match_data['team2']}",
            f"EVENT: {match_data['event']}",
            f"WINNER: {match_data['winner']}",
        ]

        # Tournament context injection
        mctx = (match_data.get('metadata') or {}).get('match_context') or {}
        if not mctx:
            # Also check directly on match_data (from extract_match_data)
            mctx = match_data.get('match_context', {})
        if mctx:
            if mctx.get('stage_label'):
                prompt_parts.append(f"STAGE: {mctx['stage_label']}")
            if mctx.get('format'):
                prompt_parts.append(f"FORMAT: {mctx['format']}")
            if mctx.get('stakes_context'):
                prompt_parts.append(f"STAKES: {'; '.join(mctx['stakes_context'][:2])}")
            if mctx.get('editorial_headlines'):
                prompt_parts.append(f"NARRATIVE: {mctx['editorial_headlines'][0]}")
            prompt_parts.append("")
            prompt_parts.append("LEAD WITH THE STAKES. What does this loss/win MEAN? Elimination? Lower bracket? Major qualification? Say it first, score second.")

        rating_context = match_data.get('rating_context', {}) if isinstance(match_data.get('rating_context'), dict) else {}
        if rating_context:
            summary_line = rating_context.get('summary_line')
            if summary_line:
                prompt_parts.append(f"IN-HOUSE RATING: {summary_line}")

            winner_key = canonicalize_team_name(match_data.get('winner', ''))
            favorite_key = rating_context.get('favorite_key')
            favorite_name = rating_context.get('favorite')
            favorite_prob = rating_context.get('favorite_win_probability_pct')
            rating_gap = float(rating_context.get('rating_gap', 0) or 0)
            if winner_key and favorite_key and favorite_name and favorite_prob:
                if winner_key == favorite_key:
                    prompt_parts.append(
                        f"Our in-house board had {favorite_name} at {favorite_prob}% pre-match on a {rating_gap:.0f}-point edge. If it fits, note that the rating edge held."
                    )
                elif rating_gap >= 25:
                    prompt_parts.append(
                        f"Our in-house board had {favorite_name} at {favorite_prob}% pre-match on a {rating_gap:.0f}-point edge, so this lands as an upset versus our own rating. Mention the upset angle only if the match actually warranted it."
                    )

        if match_data['maps']:
            prompt_parts.append(f"MAPS: {match_data['maps']}")

        if match_data['content']:
            prompt_parts.append(f"DETAILS: {match_data['content'][:500]}")

        if match_data['team_sentiment']:
            for team, vibe in match_data['team_sentiment'].items():
                prompt_parts.append(f"CROWD ON {team}: {vibe}")

        if match_data['top_comments']:
            prompt_parts.append(f"TOP HLTV COMMENT: \"{match_data['top_comments'][0]}\"")

        if match_data['memes']:
            prompt_parts.append(f"THREAD MEMES: {', '.join(match_data['memes'][:3])}")

        # Prediction callback
        if prediction_info:
            if prediction_info['accuracy'] == 'correct':
                prompt_parts.extend([
                    "",
                    f"WE CALLED IT ✅ — Our earlier tweet: \"{prediction_info['content'][:100]}\"",
                    "Include a brief callback to our correct prediction. Don't be obnoxious, just confident.",
                ])
            elif prediction_info['accuracy'] == 'wrong':
                prompt_parts.extend([
                    "",
                    f"WE GOT THIS ONE WRONG — Our earlier tweet: \"{prediction_info['content'][:100]}\"",
                    "Acknowledge the miss honestly. Self-awareness is more respected than hiding it.",
                ])

        # Determine if this is a big enough match for a thread
        is_upset = self._detect_upset(match_data)
        is_major_match = self._is_major_match(match_data)

        if is_major_match or is_upset:
            prompt_parts.extend([
                "",
                "This is a SIGNIFICANT match. Generate a 2-tweet thread:",
                "Tweet 1: The result + immediate reaction (max 280 chars)",
                "Tweet 2: The stats that tell the story. Mention specific player ratings, map scores, or round differences. If stats aren't available, break down WHAT went wrong/right tactically. (max 280 chars)",
                "",
                "Format: tweet1|||tweet2",
                "Each tweet should stand alone. No numbering (1/, 2/ etc).",
                "Tweet 2 should feel like a stats breakdown a real CS2 analyst would post.",
            ])
        else:
            prompt_parts.extend([
                "",
                "Generate ONE precise analysis tweet (max 280 chars).",
                "Focus on WHAT HAPPENED and WHY IT MATTERS.",
                "Be specific: mention key moments, map performances, or tactical shifts if info is available.",
                "Don't just restate the score. Add insight.",
            ])

        prompt = "\n".join(prompt_parts)

        system_prompt = """You are a CS2 fan account that watches every game. You share quick emotional takes after matches.

LANGUAGE: B2 English. Simple words. Short sentences. Talk like a fan, not an ESPN commentator.

STAKES FIRST: If the match means elimination, bracket drop, Major spot, or grand final — LEAD with that.
Use CAPS on the key stakes words: ELIMINATED, OUT, LOWER BRACKET, ONE LOSS AWAY, MAJOR.
The stakes ARE the tweet. The score is secondary.

Examples of good tweets:
"FaZe are 1 MAP LOSS AWAY from MISSING THE Cologne Major. Crazy..."
"NaVi ELIMINATED from the Major. In groups. What is happening."
"Spirit dropped to the lower bracket after losing to MOUZ. Must win everything from here."
"FaZe without rain on Nuke is not the same team. 4-11 CT side. Brutal."
"Vitality 2-0 FaZe. Grand final. Best CS2 we've seen all year."

RULES:
- Max 280 chars per tweet
- 1 emoji max, at the end
- No em-dashes, no semicolons
- One idea per tweet. Keep it simple.
- If you don't have stats, just give your opinion
- NEVER make up numbers or player ratings"""

        result = self.client.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            tier='auto',
            temperature=0.7,
            max_tokens=200
        )

        text = result['text'].strip()

        # Sanitize: reject LLM output that leaks instructions instead of tweets
        leak_markers = [
            'generate a', 'write a', 'tweet 1:', 'tweet 2:', 'max 280',
            'we need to', 'we must', 'let\'s craft', 'provide result',
            'separated by', 'format:', 'rules:', 'ensure no',
            'should react', 'one idea per tweet',
        ]
        text_check = text.lower()
        if any(marker in text_check for marker in leak_markers):
            logger.warning(f"⚠️  LLM leaked instructions, retrying with simpler prompt")
            # Retry with a much simpler prompt
            retry_prompt = f"{match_data['team1']} vs {match_data['team2']}: {match_data['winner']} won {match_data.get('score', '')}. Write a short fan reaction tweet. Max 280 chars. Just the tweet text."
            retry = self.client.generate(
                prompt=retry_prompt,
                system_prompt=system_prompt,
                tier='auto',
                temperature=0.8,
                max_tokens=150
            )
            text = retry['text'].strip().strip('"')
            # If still leaking, hard-code a safe fallback
            if any(m in text.lower() for m in leak_markers):
                text = f"{match_data['winner']} take it over {match_data.get('loser', match_data['team1'])}! What a series 🔥"
                logger.warning(f"⚠️  LLM retry also leaked, using fallback")

        # Strip wrapping quotes from LLM output
        text = text.strip('"').strip("'")

        # Parse thread format
        if '|||' in text:
            tweets = [t.strip().strip('"').strip("'") for t in text.split('|||') if t.strip()]
            # Validate each tweet doesn't contain instruction leaks
            valid_tweets = []
            for t in tweets[:3]:
                t_check = t.lower()
                if not any(m in t_check for m in leak_markers) and 20 <= len(t) <= 280:
                    valid_tweets.append(t)
            if len(valid_tweets) >= 2:
                return {
                    'is_thread': True,
                    'thread_tweets': valid_tweets[:3],
                    'final_text': valid_tweets[0],
                    'model': result.get('model', 'unknown'),
                    'is_upset': is_upset,
                }

        return {
            'is_thread': False,
            'thread_tweets': None,
            'final_text': text[:280],
            'model': result.get('model', 'unknown'),
            'is_upset': is_upset,
        }

    def _detect_upset(self, match_data: Dict[str, Any]) -> bool:
        """Detect if match result was an upset based on team tiers"""
        t1 = match_data['team1'].lower()
        t2 = match_data['team2'].lower()
        winner = match_data['winner'].lower()

        # Define rough tier hierarchy
        tier1_teams = {'natus vincere', 'navi', 'vitality', 'faze', 'faze clan',
                       'g2', 'g2 esports', 'spirit', 'team spirit', 'mouz', 'mousesports'}
        tier2_teams = {'liquid', 'team liquid', 'heroic', 'fnatic', 'furia',
                       'eternal fire', 'virtus.pro', 'cloud9', 'complexity',
                       'astralis', 'big', '3dmax'}

        # Upset: lower tier beats higher tier
        winner_is_t1 = any(t in winner for t in tier1_teams)
        winner_is_t2 = any(t in winner for t in tier2_teams)
        loser = t1 if winner != t1 else t2
        loser_is_t1 = any(t in loser for t in tier1_teams)
        loser_is_t2 = any(t in loser for t in tier2_teams)

        # T2 beating T1 = upset
        if (winner_is_t2 or not winner_is_t1) and loser_is_t1:
            return True
        # Unknown team beating T1/T2 = upset
        if not winner_is_t1 and not winner_is_t2 and (loser_is_t1 or loser_is_t2):
            return True

        return False

    def _is_major_match(self, match_data: Dict[str, Any]) -> bool:
        """Check if match is major/playoff caliber"""
        event = match_data['event'].lower()
        # Grand finals, semifinals, playoff matches
        major_keywords = ['major', 'grand final', 'semifinal', 'playoff', 'champions stage']
        if any(k in event for k in major_keywords):
            return True
        # Check match context for high-stakes stages
        mctx = match_data.get('match_context', {})
        stage = mctx.get('stage_type', '')
        if stage in ('grand_final', 'semifinal', 'elimination', 'decider', 'winners_match'):
            return True
        # Both teams are T1
        t1 = match_data['team1'].lower()
        t2 = match_data['team2'].lower()
        top_teams = {'navi', 'natus vincere', 'vitality', 'faze', 'spirit', 'g2', 'mouz'}
        t1_top = any(t in t1 for t in top_teams)
        t2_top = any(t in t2 for t in top_teams)
        return t1_top and t2_top

    async def analyze_match(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Full post-match analysis pipeline.
        Called by tweet_scheduler when a match_result event is processed.

        Returns analysis result dict or None if not T1-worthy.
        """
        if not self.is_t1_match(event):
            logger.info(f"⏭️  Not a T1 match, skipping analysis")
            return None

        match_data = self.extract_match_data(event)
        logger.info(f"🔬 Analyzing: {match_data['team1']} {match_data['score1']}-{match_data['score2']} {match_data['team2']} ({match_data['event']})")

        # Check if we made predictions about this match
        prediction_info = None
        predictions = self.find_our_predictions(match_data['team1'], match_data['team2'])
        if predictions:
            pred = predictions[0]  # Most recent
            accuracy = self.check_prediction_accuracy(
                pred['content'], match_data['winner'],
                match_data['team1'], match_data['team2']
            )
            if accuracy:
                prediction_info = {
                    'content': pred['content'],
                    'twitter_tweet_id': pred['twitter_tweet_id'],
                    'accuracy': accuracy,
                }
                logger.info(f"📊 Found our prediction: {accuracy.upper()} — \"{pred['content'][:60]}\"")

        # Generate analysis
        analysis = self.generate_analysis(match_data, prediction_info)

        # Add metadata
        analysis['match_data'] = match_data
        analysis['prediction_info'] = prediction_info

        logger.info(f"✅ Analysis generated: \"{analysis['final_text'][:80]}...\"")
        if analysis.get('is_upset'):
            logger.info("⚡ UPSET detected — higher engagement expected")

        return analysis


# Singleton
_analyzer = None

def get_match_analyzer() -> MatchAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = MatchAnalyzer()
    return _analyzer
