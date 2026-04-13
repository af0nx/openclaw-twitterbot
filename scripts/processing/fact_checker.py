#!/usr/bin/env python3
"""
Fact Checker - Twitter Bot Pipeline V2
Validates factual accuracy before posting
Prevents hallucinated player names, scores, or financial data
"""

import logging
from typing import Dict, Any, Optional
import re

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.openrouter_client import get_openrouter_client

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class FactChecker:
    """Validates factual claims in generated content"""
    
    # Known teams, players, operators (expandable via DB)
    KNOWN_CS2_TEAMS = {
        'navi', 'vitality', 'faze', 'g2', 'liquid', 'astralis',
        'mouz', 'heroic', 'cloud9', 'fnatic', 'spirit', 'imperial',
        'complexity', 'eternal fire', 'ence', 'big', 'furia'
    }
    
    # Aliases for team name matching (short name → possible full names in sources)
    TEAM_ALIASES = {
        'navi': ['natus vincere', 'na\'vi', 'navi'],
        'faze': ['faze clan', 'faze'],
        'g2': ['g2 esports', 'g2'],
        'mouz': ['mousesports', 'mouz'],
        'spirit': ['team spirit', 'spirit'],
        'vitality': ['team vitality', 'vitality'],
        'liquid': ['team liquid', 'liquid'],
        'heroic': ['team heroic', 'heroic'],
        'cloud9': ['cloud9', 'c9'],
        'imperial': ['imperial esports', 'imperial'],
        'complexity': ['complexity gaming', 'complexity', 'col'],
        'eternal fire': ['eternal fire', 'ef'],
        'big': ['big clan', 'big '],
        'furia': ['furia esports', 'furia'],
        'astralis': ['astralis'],
        'fnatic': ['fnatic'],
        'ence': ['ence'],
    }
    
    KNOWN_PLAYERS = {
        'zywoo', 's1mple', 'niko', 'device', 'm0nesy', 'donk',
        'frozen', 'rain', 'karrigan', 'twistzz', 'elige', 'yekindar'
    }
    
    KNOWN_OPERATORS = {
        'stake', 'draftkings', 'fanduel', 'betmgm', 'bet365',
        'thunderpick', 'csgoroll', 'pinnacle', 'bovada'
    }
    
    def __init__(self):
        self.client = get_openrouter_client()
    
    def extract_entities(self, text: str) -> Dict[str, list]:
        """Extract named entities from text"""
        entities = {
            'teams': [],
            'players': [],
            'operators': [],
            'scores': [],
            'financial': []
        }
        
        text_lower = text.lower()
        
        # Common English words that are also team names — require uppercase in original text
        AMBIGUOUS_TEAMS = {'big', 'saw', 'spirit', 'pain', 'imperial'}
        
        # Extract teams
        for team in self.KNOWN_CS2_TEAMS:
            if team in AMBIGUOUS_TEAMS:
                # Must appear as uppercase/proper-case in original text (e.g., "BIG", "Spirit")
                if re.search(r'\b' + re.escape(team.upper()) + r'\b', text) or \
                   re.search(r'\b' + re.escape(team.capitalize()) + r'\b', text):
                    entities['teams'].append(team)
            elif len(team) <= 3:
                # Short non-ambiguous names (g2, c9) — word boundary match
                if re.search(r'\b' + re.escape(team) + r'\b', text_lower):
                    entities['teams'].append(team)
            else:
                if team in text_lower:
                    entities['teams'].append(team)
        
        # Extract players
        for player in self.KNOWN_PLAYERS:
            if player in text_lower:
                entities['players'].append(player)
        
        # Extract operators
        for op in self.KNOWN_OPERATORS:
            if op in text_lower:
                entities['operators'].append(op)
        
        # Extract scores (e.g., "2-0", "16-14")
        scores = re.findall(r'\b\d{1,2}-\d{1,2}\b', text)
        entities['scores'] = scores
        
        # Extract financial figures (e.g., "$100M", "€2.1B")
        financial = re.findall(r'[$€£]\s*\d+(?:\.\d+)?[KMB]?', text, re.IGNORECASE)
        entities['financial'] = financial
        
        return entities
    
    def check_team_names(self, entities: Dict[str, list], context: Dict[str, Any]) -> Dict[str, Any]:
        """Verify team names against event context"""
        issues = []
        
        # Check if teams mentioned in tweet match event metadata
        event_teams = []
        if context.get('metadata'):
            meta = context['metadata']
            # Support both {team1, team2} and {teams: [...]} formats
            if meta.get('teams'):
                event_teams = [t.lower() for t in meta['teams'] if t]
            else:
                t1 = meta.get('team1', '').lower()
                t2 = meta.get('team2', '').lower()
                event_teams = [t for t in [t1, t2] if t]
        
        # Also check headline and content for team names as fallback
        if not event_teams:
            source = ((context.get('headline') or '') + ' ' + (context.get('content') or '')).lower()
            for team in entities['teams']:
                # Check direct match OR any alias match
                if team in source:
                    event_teams.append(team)
                else:
                    aliases = self.TEAM_ALIASES.get(team, [])
                    if any(alias in source for alias in aliases):
                        event_teams.append(team)
        
        for team in entities['teams']:
            if not event_teams:
                continue
            # Use substring + alias matching (handles 'spirit' vs 'team spirit', 'navi' vs 'natus vincere')
            matched = False
            for et in event_teams:
                if team in et or et in team:
                    matched = True
                    break
                aliases = self.TEAM_ALIASES.get(team, [])
                if any(alias in et or et in alias for alias in aliases):
                    matched = True
                    break
            if not matched:
                issues.append(f"Team '{team}' not in event context")
        
        return {
            'valid': len(issues) == 0,
            'issues': issues
        }
    
    def check_scores(self, entities: Dict[str, list], context: Dict[str, Any]) -> Dict[str, Any]:
        """Verify match scores against event metadata"""
        issues = []
        
        if entities['scores'] and context.get('metadata'):
            meta = context['metadata']
            score1 = str(meta.get('score1', ''))
            score2 = str(meta.get('score2', ''))
            # Only validate if both score fields are actually present
            if score1 and score2:
                expected_score = f"{score1}-{score2}"
                if expected_score not in entities['scores']:
                    issues.append(f"Score mismatch. Tweet has {entities['scores']}, expected {expected_score}")
        
        return {
            'valid': len(issues) == 0,
            'issues': issues
        }
    
    def check_financial_data(self, entities: Dict[str, list], context: Dict[str, Any]) -> Dict[str, Any]:
        """Validate financial figures (prevents hallucination)"""
        issues = []
        
        # If financial data is mentioned, it should be in the source content
        if entities['financial']:
            source_content = (context.get('content') or '') + (context.get('headline') or '')
            
            for figure in entities['financial']:
                # Normalize for comparison
                normalized_figure = figure.replace(' ', '')
                if normalized_figure not in source_content:
                    issues.append(f"Financial figure '{figure}' not found in source")
        
        return {
            'valid': len(issues) == 0,
            'issues': issues
        }
    
    def llm_fact_check(self, tweet: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Use LLM to verify factual accuracy"""
        system_prompt = """You are a strict fact-checker for CS2 esports tweets.
Compare the tweet against the source content.
ONLY flag errors if the tweet contains a SPECIFIC CLAIM (name, number, score, team)
that DIRECTLY CONTRADICTS the source content.
If the source simply doesn't mention something, that is NOT an error.
If you are unsure, default to {"accurate": true, "errors": []}.

Respond with JSON only:
{"accurate": boolean, "errors": ["list of errors if any"]}"""
        
        import json as _json
        metadata_str = _json.dumps(context.get('metadata', {}), default=str)
        prompt = f"""SOURCE CONTENT:
Headline: {context.get('headline') or ''}
Content: {context.get('content') or ''}
Metadata: {metadata_str}

TWEET DRAFT:
"{tweet}"

Does the tweet directly contradict the source? (JSON only):"""
        
        try:
            result = self.client.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                tier='auto',
                temperature=0.1,
                max_tokens=200
            )
            
            import json
            response = json.loads(result['text'])
            
            return {
                'valid': response.get('accurate', True),
                'issues': response.get('errors', [])
            }
            
        except Exception as e:
            logger.warning(f"⚠️  LLM fact check failed: {e}")
            return {'valid': True, 'issues': []}  # Fail open
    
    def check(self, tweet: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run full fact-checking pipeline
        
        Returns:
            Dict with 'valid', 'issues', 'warnings'
        """
        logger.info("🔍 Running fact check...")
        
        entities = self.extract_entities(tweet)
        
        # Run all checks
        checks = {
            'teams': self.check_team_names(entities, context),
            'scores': self.check_scores(entities, context),
            'financial': self.check_financial_data(entities, context),
            'llm': self.llm_fact_check(tweet, context)
        }
        
        # Aggregate results
        all_issues = []
        for check_name, result in checks.items():
            if not result['valid']:
                all_issues.extend([f"[{check_name}] {issue}" for issue in result['issues']])
        
        valid = len(all_issues) == 0
        
        logger.info(f"🔍 Fact check complete: {'✅ Valid' if valid else '❌ Issues found'}")
        
        return {
            'valid': valid,
            'issues': all_issues,
            'entities_found': entities,
            'checks_run': list(checks.keys())
        }


# Singleton instance
_checker = None

def get_fact_checker() -> FactChecker:
    """Get or create fact checker singleton"""
    global _checker
    if _checker is None:
        _checker = FactChecker()
    return _checker


if __name__ == '__main__':
    # Test the fact checker
    checker = get_fact_checker()
    
    context = {
        'headline': 'Vitality defeats NaVi 2-0 in IEM Sydney',
        'content': 'Team Vitality secured a 2-0 victory over Natus Vincere...',
        'metadata': {
            'team1': 'Vitality',
            'team2': 'NaVi',
            'score1': '2',
            'score2': '0'
        }
    }
    
    test_tweets = [
        "Vitality 2-0 NaVi. ZywOo casually dropping  40 frags.",  # Should pass
        "FaZe 2-1 Liquid in a thriller.",  # Should fail (wrong teams)
        "Vitality with the clean sweep. $2M prize secured."  # Should flag financial data
    ]
    
    for tweet in test_tweets:
        print(f"\n🧪 Testing: {tweet}")
        result = checker.check(tweet, context)
        print(f"   Valid: {result['valid']}")
        if result['issues']:
            print(f"   Issues: {result['issues']}")
