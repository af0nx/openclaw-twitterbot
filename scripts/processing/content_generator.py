#!/usr/bin/env python3
"""
Content Generator - Twitter Bot Pipeline V2
Three-Agent Writer's Room for high-quality tweet generation.

Agent A (Writer)  — powered by xai/grok-4-1-fast-reasoning (thinks, then writes)
Agent B (Editor)  — tears it apart, no mercy
Agent C (WhimsyInjector) — makes sure it doesn't sound like it was written by HR

Powered by:
  - ClawRouter   (BlockRunAI/ClawRouter)  — 41+ models, <1ms routing
  - Siftly       (viperrcrypto/Siftly)    — bookmark/event ingestion
  - MiroFish     (666ghj/MiroFish)        — swarm consensus confidence
  - agency-agents (msitarzewski/agency-agents) — WhimsyInjector + RealityChecker patterns
"""

import logging
import re
from typing import Dict, Any, Optional
import os
import json

from dotenv import load_dotenv
import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.openrouter_client import get_openrouter_client
from processing.persona_classifier import get_persona_classifier
from processing.episodic_memory import get_episodic_memory
from ingestion.style_scraper import get_style_scraper

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ── Module-level leak markers ─────────────────────────────────────────
# Shared across all generation methods. If ANY of these appear in LLM output,
# the model echoed its instructions instead of writing a tweet.
_LEAK_MARKERS = [
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
    'as an ai', 'i cannot', "i can't", "i’m sorry", "i'm sorry",
    # Reasoning / chain-of-thought leaks (added after tweet 2042562239551418546)
    'must identify', 'need to determine', 'let me think',
    'i should', 'first,', 'step 1', 'step 2',
    'my task', 'the prompt', 'the headline', 'the event:',
    'the content:', 'the category', 'the source',
    'i\'ll write', 'i\'ll craft', 'i\'ll generate',
    'i will write', 'i will craft', 'i will generate',
    'must be under', 'keep it under', 'stay under',
    'analyzing', 'identify the', 'determine the',
]

# Regex patterns that catch structural reasoning leaks the marker list might miss.
# Match output that reads like chain-of-thought: declarative reasoning sentences,
# question-style self-prompts, or references to prompt structure.
_REASONING_PATTERNS = re.compile(
    r'(?:'
    r'^must\s+(?:identify|determine|find|check|verify|include|mention)'
    r'|^(?:need|trying|going) to (?:identify|determine|find|figure)'
    r'|^(?:first|okay|alright|so),?\s+(?:i|we|let)'
    r'|(?:the event|the headline|event data|pillar \d+)\s*[:.]'

    r')',
    re.IGNORECASE | re.MULTILINE
)

_META_RESPONSE_PATTERNS = [
    re.compile(r"it seems like there(?:'s| is) a missing element in your request", re.IGNORECASE),
    re.compile(r'\bplease provide\b.{0,80}\b(?:tweet|draft)\s+text\b', re.IGNORECASE | re.DOTALL),
    re.compile(r'\bdraft tweet text\b', re.IGNORECASE),
    re.compile(r"\byou(?:'d| would) like me to review\b", re.IGNORECASE),
    re.compile(r'\brespond with only\b', re.IGNORECASE),
    re.compile(r'\bone short reason to reject\b', re.IGNORECASE),
    re.compile(r'\byour request\b', re.IGNORECASE),
    re.compile(r'\b(?:editor feedback|realitychecker)\b', re.IGNORECASE),
]

_MAIN_FEED_PILLARS = {1, 2, 3, 4, 5, 7, 10, 13, 14, 15, 17}
_COMMENT_LIKE_OPENERS = re.compile(
    r'^\s*(?:'
    r'that|this|these|those|it|they|he|she|'
    r'books?|bookmakers?|market|public|everyone|timeline|'
    r'no way|lmao|lol|bro|still|just'
    r')\b',
    re.IGNORECASE,
)
_COMMENT_LIKE_PATTERNS = [
    re.compile(r"\bthat(?:'s| is)\s+(?:a tell|the tell|where|how|why)\b", re.IGNORECASE),
    re.compile(r'\bbooks?\s+knew\s+it\b', re.IGNORECASE),
    re.compile(r'\bmarket\s+(?:asleep|caught up|pricing|will overreact|reset)\b', re.IGNORECASE),
    re.compile(r'\bpublic\s+(?:was|is|will|still|all over|overrating|overreacting|chasing)\b', re.IGNORECASE),
    re.compile(r'\b(?:looks?|felt|feels)\s+like\s+(?:a|the)\s+(?:trap|tell)\b', re.IGNORECASE),
]
_UNPROFESSIONAL_MAIN_FEED_PATTERNS = [
    re.compile(r'^[A-Z][A-Za-z0-9&.\' -]{1,80}\s+win\s+[A-Z]', re.MULTILINE),
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
        '', cleaned, count=1, flags=re.IGNORECASE
    ).strip()

    if ((cleaned.startswith('"') and cleaned.endswith('"'))
            or (cleaned.startswith("'") and cleaned.endswith("'"))):
        cleaned = cleaned[1:-1]

    return cleaned.strip()


def main_feed_quality_issue(
    text: Optional[str],
    pillar: Optional[int] = None,
    reply_target_id: Optional[str] = None,
    quote_tweet_id: Optional[str] = None,
) -> Optional[str]:
    cleaned = normalize_generated_text(text)
    if not cleaned:
        return 'empty tweet'
    if reply_target_id or quote_tweet_id:
        return None
    try:
        pillar_int = int(pillar or 0)
    except (TypeError, ValueError):
        pillar_int = 0
    if pillar_int and pillar_int not in _MAIN_FEED_PILLARS:
        return None
    if _COMMENT_LIKE_OPENERS.search(cleaned):
        return 'main-feed copy reads like a comment'
    if any(pattern.search(cleaned) for pattern in _COMMENT_LIKE_PATTERNS):
        return 'main-feed copy reads like a comment'
    if cleaned.endswith(('?', '?!')) and pillar_int not in (13, 14):
        return 'main-feed copy reads like a casual question'
    if any(pattern.search(cleaned) for pattern in _UNPROFESSIONAL_MAIN_FEED_PATTERNS):
        return 'main-feed copy is not enterprise/professional'
    return None


def is_invalid_tweet_candidate(text: Optional[str], pillar: Optional[int] = None) -> bool:
    cleaned = normalize_generated_text(text)
    if not cleaned:
        return True

    return tweet_quality_issue(cleaned) is not None or main_feed_quality_issue(cleaned, pillar=pillar) is not None


def tweet_quality_issue(text: Optional[str]) -> Optional[str]:
    cleaned = normalize_generated_text(text)
    if not cleaned:
        return 'empty tweet'

    if len(cleaned) > 280:
        return f'tweet exceeds 280 chars ({len(cleaned)})'

    lower = cleaned.lower()
    if any(marker in lower for marker in _LEAK_MARKERS):
        return 'prompt or reasoning leakage'
    if _REASONING_PATTERNS.search(cleaned):
        return 'reasoning leakage'
    if cleaned.upper() in ('APPROVED', 'REJECTED'):
        return 'review verdict leaked'
    if cleaned.upper().startswith('APPROVED '):
        return 'review verdict leaked'
    if any(pattern.search(cleaned) for pattern in _META_RESPONSE_PATTERNS):
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


class ContentGenerator:
    """Dual-agent content generation system with ML persona selection and episodic memory"""
    
    def __init__(self):
        self.client = get_openrouter_client()
        self.persona_classifier = get_persona_classifier()
        self.episodic_memory = get_episodic_memory()
        self.style_scraper = get_style_scraper()
        self.db_conn = None
        self.system_prompt = self.load_system_prompt()
        
    def connect_db(self):
        """Establish PostgreSQL connection"""
        try:
            self.db_conn = psycopg2.connect(os.getenv('DATABASE_URL'))
            logger.info("✅ Connected to PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise
    
    def load_system_prompt(self) -> str:
        """Load the base system prompt + tunable appendix"""
        base_prompt = """You are writing for SkinBetHub, an enterprise CS2 betting intelligence and prediction-engine company.
Your job is to publish standalone main-feed posts that sound like product-led CS2 analysis, not comments under someone else's tweet.

LANGUAGE LEVEL: B2 English (upper intermediate).
- Use simple, common words. No fancy vocabulary.
- Short sentences. Easy to read. Easy to understand.
- Write like a serious CS2 product account with a clear model read. Not like a journalist. Not like a random fan reply.
- OK to use gaming slang everyone knows (clutch, choke, insane, goat, gg, rip).
- NOT OK to use rare English words, literary phrases, or complex grammar.

VOICE:
- Sound like SkinBetHub's prediction desk. Useful. Clear. Confident.
- One thought per post. Each post must stand alone on the main feed.
- You are a CS2 prediction engine and tips product. Not a reporter. Not a sportsbook ad. Not a fake insider.
- Call out book price, fair odds, model lean, model pass, map pool, player form, risk, or event stakes when the event supports it.
- Short sentences. Say less, but include the actual signal.

HARD RULES:
1. Max 280 characters. Most tweets should be 60-140 chars.
2. NEVER chain ideas with commas. Use periods. Or just stop.
3. NO em-dashes (—). NO semicolons. NO colons in the middle of a sentence.
4. NO emojis on the main account. Enterprise posts should read cleanly without reactions.
5. NO hashtags. Ever. No #CS2, no #anything.
6. One idea per tweet. Not two or three.
7. ONLY use facts from the EVENT data. Never make up stats or numbers.
8. No numbers? No problem. Use a map reason, player matchup, risk, result recap, or wait-for-line framing. Do not fake data.
9. ALWAYS IDENTIFY WHO YOU'RE TALKING ABOUT. If you mention a team or player, make sure the reader knows who they are.
   - Lesser-known teams: add context like "tier 2 team" or their region.
   - Lesser-known players: mention their team name. "K27's AWPer" not just a random name.
   - If the event mentions forfeits, eliminations, etc: NAME THE TEAMS. "Three forfeits on day one" is useless. "MOUZ, Vitality, and Spirit all forfeited on day one" is clear.
   - NEVER post a tweet where a casual reader would ask "who?" or "what team?"
   - If you don't have enough info to identify teams/players, use the info you DO have. Don't post vague tweets.
10. NEVER claim secret info, fixed matches, or guaranteed edges. No fake insider talk.
11. MAIN-FEED POSTS MUST NOT READ LIKE COMMENTS.
   - Do not start with "That", "This", "Market", "Public", "Books", "Everyone", "No way", "Still", or "Just".
   - Start with a team, player, event, or "SkinBetHub model".
   - Bad: "That's a tell. Market pricing them too high."
   - Good: "Vitality vs NAVI: low-energy media comments add risk to the favorite price."
12. Do not use empty betting filler: "market asleep", "books knew it", "public trap", "priced wrong", "that's a tell".
13. ENTERPRISE STANDARD:
   - No jokes, punchlines, memes, or reaction endings.
   - No rhetorical questions.
   - Do not use words like "just", "cruel", "mirage", "lmao", "lol", or "bro".
   - Write the caption that belongs beside a real CS2 photo or useful visual.

TONE:
- Write like a CS2 prediction desk that actually watches the games.
- Clear model read. Not "according to my analysis" energy.
- If the news is crazy just say it simply. The fact IS the content.
- No meme endings. No fan-reply tone.
- Never say: "degens", "cashing", "fodder", "chalk", "bloodbath", "yeets", "implications", "significant"
- If the book price is slow, say what team/event caused it.
- If the public is overreacting, name the result or map they are overreacting to.
- Never sound like a sports reporter or casino promo bot.

PRODUCT-LED FORMAT:
- Strong main-feed posts usually contain:
  Team/Event: signal.
  Why it matters: price, map, player, risk, or model pass.
- Prediction posts should prefer:
  Team vs Team
  Model lean/pass: Team ML
  Book 1.92 | Fair 1.85 | Edge +2.1 pts
  Why: one grounded reason

STAKES & ENERGY:
- If a team is ELIMINATED, dropped to lower bracket, or fighting for Major spots — LEAD WITH THE STAKES.
- "FaZe dropped to lower bracket" is boring. "FaZe are ONE LOSS from going HOME" is fire.
- Big events (Majors, IEM, BLAST) deserve more energy than random online matches.
- Use CAPS for emphasis on key words (ONE, ELIMINATED, MAJOR, OUT). Not full sentences.
- If it's a grand final, elimination match, or Major qualifier — the stakes ARE the content.
- If the event changes the market, public sentiment, or likely price — lead with that shift.

GROUNDING:
- Build every tweet from the EVENT data you get
- Match result? Lead with what it MEANS (elimination? bracket drop? title?), then who won.
- News? Say what happened. Add your take if you have one.
- VIP reply? Answer what they actually said. Be natural. Replies may be casual.
- If the headline contains a direct QUOTE from a player, USE the strongest part of that quote in your tweet. Don't just say "heavy quote" or "tough words" — include the actual words.
- ALWAYS use the actual team name. Never write "his team" or "their team" — write the real name (e.g. "EYEBALLERS" not "JW's team").
- If you mention odds, market moves, price, value, or the public side, it must be grounded in the event data or direct context. Never invent a line move.

OUTPUT:
- Output ONLY the tweet text. Nothing else.
- Never output instructions, labels, explanations, or meta-commentary.
- Never start with "Tweet:", "Here's", "Sure", or similar preambles.

"""
        # Try to load tunable appendix from RLHF system (validated by claude-sonnet-4.6)
        try:
            appendix_path = Path(__file__).parent.parent.parent / 'config' / 'system_prompt_appendix.txt'
            if appendix_path.exists():
                with open(appendix_path) as f:
                    appendix = f.read().strip()
                    base_prompt += f"\nRLHF TUNING (claude-sonnet-4.6 approved):\n{appendix}\n"
        except Exception as e:
            logger.warning(f"⚠️  Could not load RLHF appendix: {e}")
        
        return base_prompt
    
    # Style examples are injected in the USER prompt (not system) to prevent LLM echoing
    STYLE_EXAMPLES = """STYLE REFERENCE (study the energy but do NOT copy or mention these):
    "NaVi vs FaZe: model pass unless the map veto gives NaVi Mirage control."
    "Vitality closed like a title favorite. The only risk was a quiet ZywOo map and it never came."
    "G2 looked clean today. Vitality is the real price test. I want the opener before touching it."
    "Astralis still get name-brand respect. The late rounds do not deserve that price."
    "One bad map is not a new fair line. That is how people overpay."
    "No book line attached yet. Waiting for the veto before calling this a bet."
    "Spirit result was strong. The next price depends on whether books tax the hype."
    "That roster move is not just news. It changes the map pool and the fair price."
"""
    
    def generate_writer_draft(self, event: Dict[str, Any], pillar: int) -> str:
        """Agent A: The Writer - Generate initial draft with ML persona + episodic memory"""
        
        pillar_context = {
            1: "CS2 news. Write a standalone SkinBetHub insight. Say what happened and what signal it changes: price, map, player, risk, or model pass.",
            2: "Match result. Lead with the stakes, result recap, or pricing lesson. Then who won.",
            3: "Hot take. One strong betting intelligence point with a concrete anchor: odds, map, player, risk, or result.",
            5: "Meme moment. If it's funny just show it. Don't explain.",
            7: "Drama. What happened and what it changes for the team, map pool, price, or risk.",
            12: "VIP reply. Answer what they said. Sound like a sharp trader. Simple English.",
            13: "Poll question. Make people choose sides like a market and argue in replies.",
            14: "Conversation starter. Start a market argument. Get people replying.",
            15: "Style-banked take. Standalone SkinBetHub insight. Enterprise prediction voice, not a comment.",
            16: "Disagreement reply. Push back with one grounded stat or one market angle. Be sharp. Not angry.",
        }
        
        context = pillar_context.get(pillar, "CS2 esports content")
        
        # ML persona selection
        category = event.get('category', 'general')
        urgency = event.get('urgency', 'normal')
        persona_result = self.persona_classifier.classify(category=category, urgency=urgency)
        persona_name = persona_result['persona']
        persona_desc = persona_result['description']
        persona_method = persona_result['method']
        logger.info(f"🎭 Persona: {persona_name} ({persona_method}, conf={persona_result['confidence']:.2f})")
        
        # Episodic memory injection for VIP replies
        memory_block = ""
        vip_author = event.get('metadata', {}).get('vip_author') if isinstance(event.get('metadata'), dict) else None
        if pillar == 12 and vip_author:
            memories = self.episodic_memory.recall_vip_context(vip_author, event.get('headline', ''))
            memory_block = self.episodic_memory.format_for_prompt(memories)
        elif pillar in (3, 10, 16):
            memories = self.episodic_memory.recall_topic_context(event.get('headline', ''))
            memory_block = self.episodic_memory.format_for_prompt(memories)
        
        # Community vibe injection (from HLTV comment scraping)
        community_block = ""
        metadata = event.get('metadata') or {}
        if isinstance(metadata, dict):
            vibe = metadata.get('community_vibe') or {}
            memes = vibe.get('memes', [])
            top_comments = vibe.get('top_comments', [])
            team_sentiment = vibe.get('team_sentiment', {})
            
            if memes or top_comments or team_sentiment:
                community_parts = ["HLTV COMMUNITY VIBE:"]
                if memes:
                    community_parts.append(f"  Memes in the thread: {', '.join(memes[:5])}")
                if team_sentiment:
                    for team, vibe_label in team_sentiment.items():
                        community_parts.append(f"  {team}: crowd is {vibe_label}")
                if top_comments:
                    community_parts.append(f"  Top comment: \"{top_comments[0]}\"")
                community_parts.append("Use this to make the tweet feel like it comes from someone who read the whole thread.")
                community_block = '\n'.join(community_parts)
        
        # Style bank injection: real viral tweets as few-shot examples
        style_block = ""
        try:
            self.style_scraper.connect_db()
            style_examples = self.style_scraper.get_style_examples(count=5)
            if style_examples:
                style_block = "VIRAL CS2 TWEETS (study the format, length, energy):\n" + "\n".join(f"  {ex}" for ex in style_examples)
        except Exception:
            pass  # Style bank is optional

        prompt_parts = [
            f"PILLAR {pillar}: {context}",
            self.STYLE_EXAMPLES,
        ]
        if style_block:
            prompt_parts.append(f"\n{style_block}")
        if memory_block:
            prompt_parts.append(f"\n{memory_block}")
        if community_block:
            prompt_parts.append(f"\n{community_block}")
        if pillar == 12 and event.get('content'):
            # VIP reply: make it clear we're replying to their actual tweet
            vip_user = event.get('metadata', {}).get('vip_username', '') if isinstance(event.get('metadata'), dict) else ''
            prompt_parts.extend([
                f"\nVIP TWEET TO REPLY TO:",
                f"@{vip_user} said: \"{event.get('content', '')}\"",
                f"\nWrite a reply that directly responds to what they said. Max 280 chars. Just the tweet text.",
            ])
        elif pillar == 16 and event.get('content'):
            metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
            target_user = metadata.get('target_username', '')
            target_kind = metadata.get('target_type', 'post')
            focus = metadata.get('focus', '')
            evidence_lines = metadata.get('evidence_lines') or []
            evidence_block = ""
            if evidence_lines:
                evidence_block = "\nEVIDENCE YOU CAN USE:\n" + "\n".join(
                    f"- {line}" for line in evidence_lines[:3] if line
                )
            prompt_parts.extend([
                f"\nCOMMUNITY {target_kind.upper()} TO RESPOND TO:",
                f"@{target_user} said: \"{event.get('content', '')}\"",
            ])
            if focus:
                prompt_parts.append(f"ANGLE: {focus}")
            if evidence_block:
                prompt_parts.append(evidence_block)
            prompt_parts.extend([
                "",
                "Reply with a clean disagreement.",
                "Use one stat OR one grounded statement from the evidence when it helps.",
                "Do not insult them. Do not sound corporate. Do not write an essay.",
                "Max 220 chars. Just the reply text.",
            ])
        else:
            prompt_parts.extend([
                f"\nEVENT:",
                f"Headline: {event.get('headline', '')}",
                f"Content: {event.get('content', '')}",
                f"Category: {category}",
            ])
            # Inject tournament stakes context if available
            if isinstance(metadata, dict) and metadata.get('match_context'):
                mctx = metadata['match_context']
                stakes_parts = []
                if mctx.get('stage_label'):
                    stakes_parts.append(f"Stage: {mctx['stage_label']}")
                if mctx.get('format'):
                    stakes_parts.append(f"Format: {mctx['format']}")
                if mctx.get('stakes_context'):
                    stakes_parts.append(f"What's at stake: {'; '.join(mctx['stakes_context'][:2])}")
                if mctx.get('editorial_headlines'):
                    stakes_parts.append(f"Narrative: {mctx['editorial_headlines'][0]}")
                if stakes_parts:
                    prompt_parts.append(f"\nTOURNAMENT CONTEXT:")
                    prompt_parts.extend(f"  {s}" for s in stakes_parts)
                    prompt_parts.append("Lead with the STAKES. What does this result mean for the team? Elimination? Bracket drop? Major spot?")
            prompt_parts.append(f"\nGenerate ONE tweet (max 280 chars). No explanation, just the tweet text.\nNEVER start with 'We need to', 'Let\\'s', 'I\\'ll' or any reasoning. Output ONLY the tweet.")
        prompt = "\n".join(prompt_parts)
        
        # Inject persona into system prompt (NOT user prompt) to prevent LLM from echoing it
        writer_system = self.system_prompt + f"\n\nYour writing persona: {persona_name} — {persona_desc}. Adopt this voice but NEVER mention the persona name or description in your output."
        
        result = self.client.generate(
            prompt=prompt,
            system_prompt=writer_system,
            tier='auto',
            temperature=0.8,
            max_tokens=100
        )
        self._last_writer_model = result.get('model', 'unknown')

        return normalize_generated_text(result['text'])
    
    def generate_editor_feedback(self, draft: str, pillar: int) -> str:
        """Agent B: The Editor (RealityChecker) - Critique the draft.
        
        Inspired by agency-agents RealityChecker pattern:
        github.com/msitarzewski/agency-agents
        """
        
        editor_prompt = """You are the Editor. Check if this CS2 tweet is good enough to post.

Language must be B2 English (upper intermediate). Simple words. Short sentences.

REJECT if ANY of these:
- More than 1 comma → REJECT (use periods instead)
- Has em-dashes (—) → REJECT
- Over 280 chars → REJECT
- Sounds like a journalist, desk segment, or fake TV analyst → REJECT
- Uses hard words like "implications", "significant", "devastating", "unprecedented" → REJECT
- Uses slang like "degens", "cashing", "fodder", "chalk", "bloodbath" → REJECT
- Chains ideas with commas → REJECT
- Press release tone ("It's official!", "Big news!") → REJECT
- Main-feed copy that sounds like a reply or live-chat comment → REJECT
- Starts with "That", "This", "Market", "Public", "Books", "Everyone", "No way", "Still", or "Just" on pillars other than replies → REJECT
- Empty betting filler like "market asleep", "books knew it", "public trap", "priced wrong", or "that's a tell" → REJECT
- Any emoji on a main-feed post → REJECT
- Rhetorical question or punchline ending → REJECT
- Words like "just", "cruel", "mirage", "lmao", "lol", or "bro" → REJECT
- Made-up stats or numbers → REJECT
- Claims insider info, fixed games, guaranteed edges, or fake line moves → REJECT
- Would a real CS2 fan cringe at this? → REJECT
- Mentions a player or team without identifying them → REJECT. A casual CS2 fan must know WHO the tweet is about. If the tweet says a name like "Marsborne" without their team, REJECT. If it says "Three forfeits" without naming the teams, REJECT.
- Says "his team", "their team", "JW's team" instead of using the ACTUAL team name → REJECT. Use the real name.
- References a quote ("heavy quote", "tough words") without including the actual quote or key phrase → REJECT. If there's a quote, USE IT.

ALLOW these when grounded in the event or clear context:
- market
- price
- line move
- public side
- overreaction
- value
- buy low / sell high

APPROVE if it sounds like a standalone enterprise SkinBetHub main-feed insight. Short. Professional. Simple English. One concrete signal.

Respond with ONLY: "APPROVED" or one short reason to reject."""
        
        prompt = f"""DRAFT TWEET (Pillar {pillar}):
"{draft}"

Your critique:"""
        
        result = self.client.generate(
            prompt=prompt,
            system_prompt=editor_prompt,
            tier='eco',
            temperature=0.3,
            max_tokens=150
        )
        
        return result['text'].strip()
    
    def generate_revision(self, original_draft: str, feedback: str, event: Dict[str, Any]) -> str:
        """Agent A: The Writer - Revise based on feedback"""
        
        prompt = f"""ORIGINAL DRAFT:
"{original_draft}"

EDITOR FEEDBACK:
{feedback}

EVENT CONTEXT:
{event.get('headline', '')}

Revise the tweet addressing the feedback. Max 280 chars. Output ONLY the revised tweet text. No explanation, no thinking, no labels. NEVER start with 'We need to', 'Let's', 'I'll' or any reasoning."""
        
        result = self.client.generate(
            prompt=prompt,
            system_prompt=self.system_prompt,
            tier='auto',
            temperature=0.7,
            max_tokens=100
        )

        return normalize_generated_text(result['text'])

    def force_clean_rewrite(self, event: Dict[str, Any], pillar: int, bad_output: str) -> str:
        metadata = event.get('metadata') or {}
        category = event.get('category', 'general')

        prompt_parts = [
            'Your last output was meta-commentary or review text. Rewrite it as a real tweet.',
            f'BAD OUTPUT:\n"{bad_output}"',
            f'PILLAR {pillar}',
        ]

        if pillar == 12 and event.get('content'):
            vip_user = metadata.get('vip_username', '') if isinstance(metadata, dict) else ''
            prompt_parts.extend([
                'VIP TWEET TO REPLY TO:',
                f'@{vip_user} said: "{event.get("content", "")}"',
            ])
        elif pillar == 16 and event.get('content'):
            target_user = metadata.get('target_username', '') if isinstance(metadata, dict) else ''
            prompt_parts.extend([
                'COMMUNITY POST TO RESPOND TO:',
                f'@{target_user} said: "{event.get("content", "")}"',
            ])
        else:
            prompt_parts.extend([
                'EVENT:',
                f'Headline: {event.get("headline", "")}',
                f'Content: {event.get("content", "")}',
                f'Category: {category}',
            ])

        prompt_parts.extend([
            'Rules:',
            '- Output ONLY the final tweet text.',
            '- Do NOT mention request, draft, review, approval, rejection, or feedback.',
            '- Do NOT explain yourself.',
            '- Max 280 chars.',
            '- Write standalone SkinBetHub prediction-engine copy, not a reply-style comment.',
            '- Start with a team, player, event, or SkinBetHub model angle.',
            '- Grounded price/model language is allowed. Fake insider claims are not.',
        ])

        result = self.client.generate(
            prompt='\n'.join(prompt_parts),
            system_prompt=self.system_prompt,
            tier='auto',
            temperature=0.4,
            max_tokens=100,
        )
        return normalize_generated_text(result['text'])
    
    def generate_whimsy_check(self, draft: str, pillar: int, community_vibe: dict) -> str:
        """Agent C: WhimsyInjector — ensures the tweet has personality, not just accuracy.
        
        Inspired by agency-agents WhimsyInjector pattern:
        github.com/msitarzewski/agency-agents
        
        Only triggers on dry/safe drafts. APPROVED drafts pass through unchanged.
        Runs on PREMIUM tier (claude-sonnet-4.6) — best qualitative judgment.
        """
        memes = community_vibe.get('memes', [])
        swarm_score = community_vibe.get('swarm_confidence', 0.5)
        
        whimsy_prompt = f"""You are the WhimsyInjector. Make sure this tweet has personality.

You are checking a CS2 tweet for @SkinBetHub.

TASK: Does the tweet already sound fun and natural? If yes → return "APPROVED".
If it's boring or too generic → make ONE small edit to give it life.

RULES:
- Stay under 280 chars
- Keep the main fact
- Use B2 English only. Simple words. No fancy vocabulary.
- No new emojis unless it really fits
- If it's already good, say APPROVED. Don't change good tweets.
- Available memes from HLTV thread: {', '.join(memes[:4]) if memes else 'none'}
- Swarm confidence: {swarm_score:.2f}

Draft to check:
"{draft}"

Respond with ONLY: "APPROVED" or the better tweet text (no explanation)."""
        
        result = self.client.generate(
            prompt=whimsy_prompt,
            system_prompt="You are a wit consultant for a CS2 Twitter account. Be surgical.",
            tier='premium',
            temperature=0.6,
            max_tokens=120
        )
        
        text = result['text'].strip().strip('"')
        # If the model returned something clearly marked approved
        if text.upper().startswith('APPROVED'):
            logger.info("✨ WhimsyInjector: already has personality")
            return draft
        # If it returned a new tweet (not just "approved")
        if len(text) >= 20 and len(text) <= 300:
            # Guard: WhimsyInjector can also leak reasoning
            if any(m in text.lower() for m in _LEAK_MARKERS) or _REASONING_PATTERNS.search(text):
                logger.warning(f"⚠️  WhimsyInjector leaked reasoning — keeping original draft")
                return draft
            logger.info(f"✨ WhimsyInjector injected: {text[:60]}...")
            return text
        return draft
    
    def dual_agent_generate(self, event: Dict[str, Any], pillar: int, max_iterations: int = 2) -> Dict[str, Any]:
        """
        Run the writer's room. Uses fast-path (1 LLM call) for VIP replies and
        engagement content. Uses full 3-agent loop for news/match tweets.
        
        Returns:
            Dict with 'final_text', 'iterations', 'approved' keys
        """
        # Fast path: engagement pillars need speed, not perfection
        # VIP replies (12) now go through editor for quality
        # 1 LLM call instead of 3-5 — critical when rate-limited to 60 req/hr
        if pillar in (13, 14):
            logger.info(f"⚡ Fast-path generation for pillar {pillar}")
            draft = normalize_generated_text(self.generate_writer_draft(event, pillar))
            logger.info(f"✍️  Fast draft: {draft[:80]}...")
            if is_invalid_tweet_candidate(draft, pillar=pillar):
                logger.warning("⚠️  Fast-path returned meta output — forcing clean rewrite")
                draft = self.force_clean_rewrite(event, pillar, draft)
            if len(draft) > 280:
                draft = draft[:277] + "..."
            if is_invalid_tweet_candidate(draft, pillar=pillar):
                logger.error(f"❌ Fast-path leaked twice — giving up: {draft[:80]}")
                return {'final_text': '', 'iterations': 2, 'approved': False, 'char_count': 0, 'model': getattr(self, '_last_writer_model', 'unknown')}
            return {
                'final_text': draft,
                'iterations': 1,
                'approved': True,
                'char_count': len(draft),
                'model': getattr(self, '_last_writer_model', 'unknown'),
            }
        
        logger.info(f"🎭 Starting 3-agent writer's room for pillar {pillar}")
        
        # Agent A: Initial draft
        draft = normalize_generated_text(self.generate_writer_draft(event, pillar))
        logger.info(f"✍️  Writer draft: {draft[:80]}...")
        if is_invalid_tweet_candidate(draft, pillar=pillar):
            logger.warning("⚠️  Writer draft was meta output — forcing clean rewrite")
            draft = self.force_clean_rewrite(event, pillar, draft)
            if is_invalid_tweet_candidate(draft, pillar=pillar):
                logger.error(f"❌ Writer draft unrecoverable — giving up: {draft[:80]}")
                return {'final_text': '', 'iterations': 1, 'approved': False, 'char_count': 0, 'model': getattr(self, '_last_writer_model', 'unknown')}
        
        iterations = 1
        approved = False
        
        for i in range(max_iterations - 1):
            # Agent B: Critique (RealityChecker)
            feedback = self.generate_editor_feedback(draft, pillar)
            logger.info(f"📝 RealityChecker #{i+1}: {feedback[:80]}...")
            
            if "APPROVED" in feedback.upper():
                approved = True
                logger.info("✅ RealityChecker approved!")
                break
            
            # Agent A: Revise
            revised = normalize_generated_text(self.generate_revision(draft, feedback, event))
            iterations += 1
            logger.info(f"✍️  Revision #{iterations}: {revised[:80]}...")
            
            # Safety: if revision leaked instructions, keep the previous draft
            if is_invalid_tweet_candidate(revised, pillar=pillar):
                logger.warning(f"⚠️  Revision leaked instructions — keeping previous draft")
            else:
                draft = revised
        
        # Agent C: WhimsyInjector pass — runs on news/match pillars to inject personality.
        # Previously gated to breaking/important only, but normal-urgency content
        # (the majority) sounded like a news aggregator without it.
        if pillar in (1, 2, 3, 7, 12):
            metadata = event.get('metadata') or {}
            community_vibe = metadata.get('community_vibe', {}) if isinstance(metadata, dict) else {}
            draft = self.generate_whimsy_check(draft, pillar, community_vibe)
        else:
            logger.info("⏭️  Skipping WhimsyInjector for engagement content")

        draft = normalize_generated_text(draft)
        if is_invalid_tweet_candidate(draft, pillar=pillar):
            logger.warning("⚠️  Final draft still looks meta — forcing clean rewrite")
            draft = self.force_clean_rewrite(event, pillar, draft)
            if is_invalid_tweet_candidate(draft, pillar=pillar):
                logger.error(f"❌ Final draft unrecoverable — giving up: {draft[:80]}")
                return {'final_text': '', 'iterations': iterations, 'approved': False, 'char_count': 0, 'model': getattr(self, '_last_writer_model', 'unknown')}
        
        # Final length check
        if len(draft) > 280:
            logger.warning(f"⚠️  Tweet too long ({len(draft)} chars), truncating...")
            draft = draft[:277] + "..."
        draft = normalize_generated_text(draft)
        
        return {
            'final_text': draft,
            'iterations': iterations,
            'approved': approved,
            'char_count': len(draft),
            'model': getattr(self, '_last_writer_model', 'unknown'),
        }
    
    def generate_thread(self, event: Dict[str, Any], pillar: int) -> Dict[str, Any]:
        """
        Generate a thread (2-3 tweets) for substantial updates like Valve patches.
        Uses the same writer's room quality but splits across multiple tweets.
        """
        logger.info(f"🧵 Generating thread for: {event.get('headline', '')[:60]}...")
        
        prompt = f"""EVENT:
Headline: {event.get('headline', '')}
Content: {event.get('content', '')[:1500]}

Write a 2-3 tweet THREAD about this CS2 update.

TWEET 1: The biggest news. Make people want to read more.
TWEET 2: The important details. What changed.
TWEET 3 (optional): Your reaction or what it means. Only if needed.

Rules:
- B2 English. Simple words. Short sentences.
- Each tweet max 270 chars
- No commas chaining ideas. One emoji max per tweet.
- Separate tweets with ---
- Just the tweet texts. No labels."""
        
        result = self.client.generate(
            prompt=prompt,
            system_prompt=self.system_prompt,
            tier='auto',
            temperature=0.7,
            max_tokens=400
        )
        
        raw = result['text'].strip()

        # Reject LLM output that leaked instructions instead of tweets
        if any(m in raw.lower() for m in _LEAK_MARKERS) or _REASONING_PATTERNS.search(raw):
            logger.warning("⚠️  Thread LLM leaked instructions, falling back to single")
            return self.dual_agent_generate(event, pillar)

        tweets = [t.strip().strip('"').strip("'") for t in raw.split('---') if t.strip()]
        
        # Validate and cap
        valid_tweets = []
        for t in tweets[:3]:
            # Skip tweets that look like instructions
            if any(m in t.lower() for m in _LEAK_MARKERS) or _REASONING_PATTERNS.search(t):
                continue
            if len(t) > 280:
                t = t[:277] + "..."
            if len(t) >= 20:
                valid_tweets.append(t)
        
        if len(valid_tweets) < 2:
            # Fallback to single tweet
            logger.warning("⚠️  Thread generation produced <2 tweets, falling back to single")
            return self.dual_agent_generate(event, pillar)
        
        return {
            'final_text': valid_tweets[0],  # First tweet is the "main" one
            'thread_tweets': valid_tweets,
            'iterations': 1,
            'approved': True,
            'is_thread': True,
            'char_count': len(valid_tweets[0])
        }
    
    def generate_for_event(self, event_id: str):
        """Generate content for a pending event"""
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT id, headline, content, category, urgency, metadata
                    FROM twitter_bot.events
                    WHERE id = %s AND status = 'pending'
                """, (event_id,))
                
                row = cur.fetchone()
                if not row:
                    logger.warning(f"⚠️  Event {event_id} not found or not pending")
                    return
                
                event = {
                    'id': str(row[0]),
                    'headline': row[1],
                    'content': row[2],
                    'category': row[3],
                    'urgency': row[4],
                    'metadata': row[5]
                }
            
            # Determine pillar based on category
            pillar_mapping = {
                'roster_change': 1,  # Breaking news
                'match_result': 2,
                'regulation': 3,     # Hot take
                'vip_engagement': 12, # VIP reply
                'community_disagreement': 16,
            }
            pillar = pillar_mapping.get(event['category'], 1)
            
            # Generate content
            result = self.dual_agent_generate(event, pillar)
            
            # Store generated tweet
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO twitter_bot.tweets_v2
                    (event_id, pillar, pillar_name, content, status, generation_model, metadata)
                    VALUES (%s, %s, %s, %s, 'draft', %s, %s)
                    RETURNING id
                """, (
                    event['id'],
                    pillar,
                    event['category'],
                    result['final_text'],
                    result.get('model', 'unknown'),
                    json.dumps({
                        'iterations': result['iterations'],
                        'approved_by_editor': result['approved'],
                        'char_count': result['char_count']
                    })
                ))
                
                tweet_id = cur.fetchone()[0]
                
                # Update event status
                cur.execute("""
                    UPDATE twitter_bot.events
                    SET status = 'generating', processed_at = NOW()
                    WHERE id = %s
                """, (event['id'],))
                
                self.db_conn.commit()
                
                logger.info(f"✅ Generated tweet {tweet_id} for event {event_id}")
                
        except Exception as e:
            logger.error(f"❌ Failed to generate content: {e}")
            self.db_conn.rollback()


if __name__ == '__main__':
    # Test dual-agent generation
    generator = ContentGenerator()
    
    test_event = {
        'headline': 'ZywOo signs 3-year extension with Vitality',
        'content': 'French superstar ZywOo has extended his contract with Team Vitality through 2027.',
        'category': 'roster_change'
    }
    
    result = generator.dual_agent_generate(test_event, pillar=1)
    
    print(f"\n✅ Dual-Agent Result:")
    print(f"Final text: {result['final_text']}")
    print(f"Iterations: {result['iterations']}")
    print(f"Approved: {result['approved']}")
    print(f"Chars: {result['char_count']}/280")
