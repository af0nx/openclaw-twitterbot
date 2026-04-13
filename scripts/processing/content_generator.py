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
        base_prompt = """You are a CS2 fan running a Twitter account. You sound like a real person who watches every match and always has something to say.

LANGUAGE LEVEL: B2 English (upper intermediate).
- Use simple, common words. No fancy vocabulary.
- Short sentences. Easy to read. Easy to understand.
- Write like you talk to a friend. Not like a journalist or analyst.
- OK to use gaming slang everyone knows (clutch, choke, insane, goat, gg, rip).
- NOT OK to use rare English words, literary phrases, or complex grammar.

VOICE:
- Sound like @Ozzny_CS2, @ThourCS2, @CS2News_EN, @RazedEsport
- One thought per tweet. React to the moment.
- You are a fan. Not a reporter. Not an analyst.
- Short sentences. Say less.

HARD RULES:
1. Max 280 characters. Most tweets should be 60-140 chars.
2. NEVER chain ideas with commas. Use periods. Or just stop.
3. NO em-dashes (—). NO semicolons. NO colons in the middle of a sentence.
4. ONE emoji max. Put it at the end. Only use: 😭🥶💀😤🔥
5. NO hashtags. Ever. No #CS2, no #anything. They look like a brand, not a fan.
6. One idea per tweet. Not two or three.
7. ONLY use facts from the EVENT data. Never make up stats or numbers.
8. No numbers? No problem. Your opinion is better than fake data.
9. ALWAYS IDENTIFY WHO YOU'RE TALKING ABOUT. If you mention a team or player, make sure the reader knows who they are.
   - Lesser-known teams: add context like "tier 2 team" or their region.
   - Lesser-known players: mention their team name. "K27's AWPer" not just a random name.
   - If the event mentions forfeits, eliminations, etc: NAME THE TEAMS. "Three forfeits on day one" is useless. "MOUZ, Vitality, and Spirit all forfeited on day one" is clear.
   - NEVER post a tweet where a casual reader would ask "who?" or "what team?"
   - If you don't have enough info to identify teams/players, use the info you DO have. Don't post vague tweets.

TONE:
- React like a real person. Surprise. Humor. Excitement.
- "no way" energy. Not "according to my analysis" energy.
- If the news is crazy just say it simply. The fact IS the content.
- Community memes are OK when natural: EZ4ENCE, cry is free, Liquid curse, rip bozo
- Never say: "degens", "cashing", "fodder", "chalk", "bloodbath", "yeets", "implications", "significant"
- Never sound like a sports reporter or betting guy

STAKES & ENERGY:
- If a team is ELIMINATED, dropped to lower bracket, or fighting for Major spots — LEAD WITH THE STAKES.
- "FaZe dropped to lower bracket" is boring. "FaZe are ONE LOSS from going HOME" is fire.
- Big events (Majors, IEM, BLAST) deserve more energy than random online matches.
- Use CAPS for emphasis on key words (ONE, ELIMINATED, MAJOR, OUT). Not full sentences.
- If it's a grand final, elimination match, or Major qualifier — the stakes ARE the content.

GROUNDING:
- Build every tweet from the EVENT data you get
- Match result? Lead with what it MEANS (elimination? bracket drop? title?), then who won.
- News? Say what happened. Add your take if you have one.
- VIP reply? Answer what they actually said. Be natural.
- If the headline contains a direct QUOTE from a player, USE the strongest part of that quote in your tweet. Don't just say "heavy quote" or "tough words" — include the actual words.
- ALWAYS use the actual team name. Never write "his team" or "their team" — write the real name (e.g. "EYEBALLERS" not "JW's team").

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
  "FaZe played against a top-1 team in ROBLOX CS and LOST 6-16 and 3-16 respectively 😭"
  "FaZe are 1 MAP LOSS AWAY from MISSING THE Cologne Major. Crazy..."
  "Spirit just got ELIMINATED from the Major. In groups. What is happening."
  "NaVi are in the lower bracket. One more loss and they're OUT. 😭"
  "m0nesy has unfollowed @FalconsEsport on Instagram right after the match today"
  "New Overpass looks so sick. I love it."
  "G2 dropped to the lower bracket after losing to MOUZ. Must win everything from here."
  "Vitality 2-0 FaZe. Grand final. This is the best CS2 we've ever seen."
"""
    
    def generate_writer_draft(self, event: Dict[str, Any], pillar: int) -> str:
        """Agent A: The Writer - Generate initial draft with ML persona + episodic memory"""
        
        pillar_context = {
            1: "CS2 news. Say what happened. React like a fan. Simple words.",
            2: "Match result. Lead with what's at stake (elimination? bracket? Major?). Then who won. Use CAPS on key stakes words. Energy.",
            3: "Hot take. One strong opinion. Say it simply.",
            5: "Meme moment. If it's funny just show it. Don't explain.",
            7: "Drama. What happened and why people care. Stay casual.",
            12: "VIP reply. Answer what they said. Be natural. Simple English.",
            13: "Poll question. Make people want to vote AND argue in replies.",
            14: "Conversation starter. Provocative question. Get people replying.",
            15: "Style-banked take. Match the energy of viral tweets that worked.",
            16: "Disagreement reply. Push back with one stat or one grounded statement. Be sharp. Not angry.",
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
        
        draft = result['text'].strip()
        # Strip common LLM preambles that leak through
        for preamble in ['Tweet:', 'Here\'s', 'Sure,', 'Here is', 'Output:', 'Draft:']:
            if draft.startswith(preamble):
                draft = draft[len(preamble):].lstrip(' :')
        # Strip instruction-echoing preambles (regex)
        draft = re.sub(
            r'^(okay,?\s*|so,?\s*|alright,?\s*|let me|i\'ll|i will|we need to|we should|let\'s)\s*.{0,60}(tweet|post|write|craft|generate)\b[^.]*\.\s*',
            '', draft, count=1, flags=re.IGNORECASE
        ).strip()
        # Strip wrapping quotes
        if (draft.startswith('"') and draft.endswith('"')) or (draft.startswith("'") and draft.endswith("'")):
            draft = draft[1:-1]
        return draft
    
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
- Sounds like a journalist or analyst → REJECT
- Uses hard words like "implications", "significant", "devastating", "unprecedented" → REJECT
- Uses slang like "degens", "cashing", "fodder", "chalk", "bloodbath" → REJECT
- Chains ideas with commas → REJECT
- Press release tone ("It's official!", "Big news!") → REJECT
- More than 1 emoji → REJECT
- Made-up stats or numbers → REJECT
- Would a real CS2 fan cringe at this? → REJECT
- Mentions a player or team without identifying them → REJECT. A casual CS2 fan must know WHO the tweet is about. If the tweet says a name like "Marsborne" without their team, REJECT. If it says "Three forfeits" without naming the teams, REJECT.
- Says "his team", "their team", "JW's team" instead of using the ACTUAL team name → REJECT. Use the real name.
- References a quote ("heavy quote", "tough words") without including the actual quote or key phrase → REJECT. If there's a quote, USE IT.

APPROVE if it sounds like a real CS2 fan tweeting. Short. Natural. Simple English. One idea.

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
        
        text = result['text'].strip()
        # Strip common LLM preambles that leak through
        for preamble in ['Tweet:', 'Here\'s', 'Sure,', 'Here is', 'Output:', 'Revised:', 'Revision:', 'Draft:']:
            if text.lower().startswith(preamble.lower()):
                text = text[len(preamble):].lstrip(' :')
        # Strip instruction-echoing preambles (regex)
        text = re.sub(
            r'^(okay,?\s*|so,?\s*|alright,?\s*|let me|i\'ll|i will|we need to|we should|let\'s)\s*.{0,60}(tweet|post|write|craft|generate)\b[^.]*\.\s*',
            '', text, count=1, flags=re.IGNORECASE
        ).strip()
        # Strip wrapping quotes
        if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
            text = text[1:-1]
        return text
    
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
            draft = self.generate_writer_draft(event, pillar)
            logger.info(f"✍️  Fast draft: {draft[:80]}...")
            if len(draft) > 280:
                draft = draft[:277] + "..."
            # Strip wrapping quotes
            if (draft.startswith('"') and draft.endswith('"')) or (draft.startswith("'") and draft.endswith("'")):
                draft = draft[1:-1]
            # Leak detection — fast path had NONE before
            if any(m in draft.lower() for m in _LEAK_MARKERS) or _REASONING_PATTERNS.search(draft):
                logger.warning(f"⚠️  Fast-path leaked instructions — retrying once")
                draft = self.generate_writer_draft(event, pillar)
                draft = draft.strip().strip('"').strip("'")
                if any(m in draft.lower() for m in _LEAK_MARKERS) or _REASONING_PATTERNS.search(draft):
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
        draft = self.generate_writer_draft(event, pillar)
        logger.info(f"✍️  Writer draft: {draft[:80]}...")
        
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
            revised = self.generate_revision(draft, feedback, event)
            iterations += 1
            logger.info(f"✍️  Revision #{iterations}: {revised[:80]}...")
            
            # Safety: if revision leaked instructions, keep the previous draft
            if any(m in revised.lower() for m in _LEAK_MARKERS) or _REASONING_PATTERNS.search(revised):
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
        
        # Final length check
        if len(draft) > 280:
            logger.warning(f"⚠️  Tweet too long ({len(draft)} chars), truncating...")
            draft = draft[:277] + "..."
        
        # Final quote stripping — LLMs love wrapping output in quotes
        if (draft.startswith('"') and draft.endswith('"')) or (draft.startswith("'") and draft.endswith("'")):
            draft = draft[1:-1]
        
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
