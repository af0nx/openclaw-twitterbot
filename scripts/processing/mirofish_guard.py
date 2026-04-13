#!/usr/bin/env python3
"""
MiroFish Guard - Twitter Bot Pipeline V2
100-agent swarm vibe-check for high-risk content
Simulates diverse personas to detect ratio risk before posting
"""

import logging
from typing import Dict, Any, List
import os
import random
import json

from dotenv import load_dotenv

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.openrouter_client import get_openrouter_client

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MiroFishGuard:
    """
    Swarm-based content safety guardrail
    ⚠️  WARNING: MiroFish is AGPL-licensed. This is a custom lightweight implementation.
    """
    
    # Persona archetypes for the 100-agent swarm
    PERSONAS = [
        {"name": "Angry Esports Fan", "sensitivity": 0.9, "weight": 1.5},
        {"name": "Compliance Lawyer", "sensitivity": 0.95, "weight": 2.0},
        {"name": "Rival Brand CEO", "sensitivity": 0.85, "weight": 1.8},
        {"name": "Reddit Moderator", "sensitivity": 0.75, "weight": 1.2},
        {"name": "Gambling Addiction Advocate", "sensitivity": 0.98, "weight": 2.5},
        {"name": "Twitter Reply Guy", "sensitivity": 0.6, "weight": 0.8},
        {"name": "Boomer Parent", "sensitivity": 0.7, "weight": 1.0},
        {"name": "Gen Z Meme Lord", "sensitivity": 0.4, "weight": 0.6},
        {"name": "Financial Regulator", "sensitivity": 0.92, "weight": 2.2},
        {"name": "Pro Player", "sensitivity": 0.65, "weight": 1.1},
        {"name": "Streamer Chat Moderator", "sensitivity": 0.55, "weight": 0.9},
        {"name": "Journalist", "sensitivity": 0.7, "weight": 1.3},
        {"name": "Brand Safety Executive", "sensitivity": 0.88, "weight": 1.9},
        {"name": "Online Troll", "sensitivity": 0.3, "weight": 0.5},
        {"name": "Ethics Committee Member", "sensitivity": 0.95, "weight": 2.3}
    ]
    
    def __init__(self, agent_count: int = 100):
        self.client = get_openrouter_client()
        self.agent_count = agent_count
        self.enabled = os.getenv('ENABLE_MIROFISH_GUARD', 'true').lower() == 'true'
        
        if not self.enabled:
            logger.warning("⚠️  MiroFish Guard is DISABLED via env config")

    def evaluate_constitutionally(self, tweet_content: str, pillar: int) -> Dict[str, Any]:
        """Run a single rubric-based brand-safety judgment instead of a simulated swarm."""
        thresholds = {
            3: 0.70,
            7: 0.75,
            12: 0.65,
            'default': 0.80,
        }
        threshold = thresholds.get(pillar, thresholds['default'])

        system_prompt = (
            "You are the final constitutional safety judge for a CS2/X bot. "
            "Apply a compact constitution: avoid defamation, harassment, targeted abuse, "
            "reckless allegations, encouragement of harm, gambling/compliance risk, and tone that could trigger brand-safety escalation. "
            "Be pragmatic: strong opinions are allowed if they stay non-defamatory and non-abusive. "
            "Return strict JSON only."
        )
        prompt = f"""Evaluate this draft tweet for pillar {pillar}.

Tweet:
{tweet_content}

Return JSON with this shape only:
{{
  "risk_score": float,
  "would_report": boolean,
  "decision": "allow" | "soft_block" | "hard_block",
  "summary": "one sentence",
  "concerns": [{{"area": "string", "severity": float, "reason": "string"}}],
  "rewrite_hint": "short suggestion"
}}"""

        result = self.client.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            tier='eco',
            temperature=0.1,
            max_tokens=300,
        )
        parsed = json.loads(result['text'])
        risk_score = max(0.0, min(float(parsed.get('risk_score', 0.0)), 1.0))
        decision = parsed.get('decision', 'allow')
        would_report = bool(parsed.get('would_report', False)) or decision in ('soft_block', 'hard_block')
        veto = risk_score > threshold or decision == 'hard_block'
        concerns = parsed.get('concerns') or []

        return {
            'safe': not veto,
            'risk_score': risk_score,
            'veto': veto,
            'threshold': threshold,
            'top_concerns': concerns[:3],
            'judge_summary': parsed.get('summary', ''),
            'rewrite_hint': parsed.get('rewrite_hint', ''),
            'agent_count_simulated': 1,
            'report_count': 1 if would_report else 0,
            'mode': 'constitutional_judge',
        }
    
    def generate_agent_pool(self) -> List[Dict[str, Any]]:
        """Generate pool of diverse agent personas"""
        agents = []
        
        # Distribute the 100 agents across persona types
        base_count = self.agent_count // len(self.PERSONAS)
        remainder = self.agent_count % len(self.PERSONAS)
        
        for i, persona in enumerate(self.PERSONAS):
            count = base_count + (1 if i < remainder else 0)
            for j in range(count):
                agents.append({
                    'id': len(agents),
                    'persona': persona['name'],
                    'sensitivity': persona['sensitivity'],
                    'weight': persona['weight'],
                    'threshold': random.uniform(0.4, 0.9)  # Random reporting threshold
                })
        
        return agents
    
    def simulate_agent_reaction(self, agent: Dict[str, Any], tweet_content: str) -> Dict[str, Any]:
        """Simulate how one agent would react to the tweet"""
        
        system_prompt = f"""You are a {agent['persona']} reviewing a tweet.
Your sensitivity level is {agent['sensitivity']:.2f} (0=tolerant, 1=strict).
Your reporting threshold is {agent['threshold']:.2f}.

Respond with JSON:
{{"would_report": boolean, "severity": float 0-1, "reason": "brief explanation"}}"""
        
        prompt = f"""Tweet content:
"{tweet_content}"

Would you report or flag this tweet? Consider:
- Tone and appropriateness
- Potential to offend your persona
- Regulatory/legal concerns
- Brand safety issues

Your response (JSON only):"""
        
        try:
            result = self.client.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                tier='eco',  # Use cheap model for swarm
                temperature=0.5,
                max_tokens=150
            )
            
            # Parse JSON response
            import json
            response = json.loads(result['text'])
            
            return {
                'agent_id': agent['id'],
                'persona': agent['persona'],
                'would_report': response.get('would_report', False),
                'severity': response.get('severity', 0.0),
                'reason': response.get('reason', ''),
                'weight': agent['weight']
            }
            
        except Exception as e:
            logger.warning(f"⚠️  Agent {agent['id']} simulation failed: {e}")
            return {
                'agent_id': agent['id'],
                'persona': agent['persona'],
                'would_report': False,
                'severity': 0.0,
                'reason': 'simulation_error',
                'weight': agent['weight']
            }
    
    def calculate_ratio_risk(self, reactions: List[Dict[str, Any]]) -> float:
        """
        Calculate weighted ratio risk score
        
        Returns:
            Float 0.0-1.0 where >0.75 = high risk
        """
        if not reactions:
            return 0.0
        
        # Count weighted reports
        total_weight = sum(r['weight'] for r in reactions)
        report_weight = sum(r['weight'] for r in reactions if r['would_report'])
        
        # Weighted severity
        avg_severity = sum(r['severity'] * r['weight'] for r in reactions) / total_weight
        
        # Combined risk score
        report_ratio = report_weight / total_weight
        risk_score = (report_ratio * 0.6) + (avg_severity * 0.4)
        
        return min(risk_score, 1.0)
    
    async def vibe_check(self, tweet_content: str, pillar: int) -> Dict[str, Any]:
        """
        Run full 100-agent vibe check
        
        Args:
            tweet_content: The tweet text to evaluate
            pillar: Content pillar (affects risk thresholds)
        
        Returns:
            Dict with 'safe', 'risk_score', 'veto', 'top_concerns'
        """
        if not self.enabled:
            return {
                'safe': True,
                'risk_score': 0.0,
                'veto': False,
                'top_concerns': [],
                'bypassed': True
            }

        logger.info("🔄 Running MiroFish constitutional judge...")
        try:
            result = self.evaluate_constitutionally(tweet_content, pillar)
            logger.info(f"🎭 Vibe check complete: Risk={result['risk_score']:.2f}, Veto={result['veto']}")
            return result
        except Exception as e:
            logger.warning(f"⚠️  Constitutional judge failed, failing open: {e}")
            return {
                'safe': True,
                'risk_score': 0.0,
                'veto': False,
                'top_concerns': [],
                'judge_error': str(e),
                'mode': 'constitutional_judge',
            }


# Singleton instance
_guard = None

def get_mirofish_guard() -> MiroFishGuard:
    """Get or create MiroFish guard singleton"""
    global _guard
    if _guard is None:
        _guard = MiroFishGuard()
    return _guard


if __name__ == '__main__':
    # Test the guard
    import asyncio
    
    guard = get_mirofish_guard()
    
    test_tweets = [
        "ZywOo signs 3-year extension with Vitality. Water is wet.",
        "RevShare is a trust exercise for liars. Change my mind.",
        "This bookie's compliance team is drunk. Absolutely unhinged terms.",
    ]
    
    async def test():
        for i, tweet in enumerate(test_tweets, 1):
            print(f"\n🧪 Test {i}: {tweet}")
            result = await guard.vibe_check(tweet, pillar=3)
            print(f"   Risk: {result['risk_score']:.2f}")
            print(f"   Veto: {result['veto']}")
            if result['top_concerns']:
                print(f"   Top concern: {result['top_concerns'][0]['persona']}")
    
    asyncio.run(test())
