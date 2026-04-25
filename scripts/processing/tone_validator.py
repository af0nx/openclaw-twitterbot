#!/usr/bin/env python3
"""
Tone Validator - Twitter Bot Pipeline V2
Ensures output doesn't sound too corporate, too AI, or violates compliance rules.

ML Enhancement: Local TF-IDF + SVM classifier trained on approved/rejected tweets.
Falls back to LLM-based tone check when local model isn't trained.
"""

import logging
import os
import pickle
import io
from typing import Dict, Any
import re

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.openrouter_client import get_openrouter_client

# ML: local tone classifier (saves LLM calls)
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.svm import LinearSVC
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.pipeline import Pipeline as SkPipeline
    import numpy as np
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv('/dev/shm/.env')
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False


# Safe deserialization: only allow sklearn/numpy types
_SAFE_MODULES = frozenset({
    'sklearn', 'numpy', 'scipy', 'collections', 'builtins',
    'copy_reg', 'copyreg', '_codecs', 'encodings',
})


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str):
        top = module.split('.')[0]
        if top in _SAFE_MODULES:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"Blocked unsafe class: {module}.{name}")


def _restricted_load(f):
    return _RestrictedUnpickler(f).load()


# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ToneValidator:
    """Validates tone and compliance of generated content.

    Uses a two-stage approach:
    1. Rule-based checks (compliance, corporate tells, AI tells, length)
    2. ML tone classifier (TF-IDF + SVM) trained on approved/rejected tweets
       Falls back to LLM eco-tier call if model isn't trained or confidence is low.
    """

    MODEL_PATH = Path(__file__).parent.parent.parent / 'models' / 'tone_classifier.pkl'
    MIN_TRAINING_SAMPLES = 50
    ML_CONFIDENCE_THRESHOLD = 0.70  # Below this, escalate to LLM

    # Forbidden patterns (compliance)
    FORBIDDEN_PATTERNS = [
        (r'\bguaranteed\b', "No absolute guarantees"),
        (r'\bwill win\b', "No absolute predictions"),
        (r'\bcan\'t lose\b', "No absolute predictions"),
        (r'\bsure thing\b', "No absolute predictions"),
        (r'\brisk-free\b', "Misleading gambling language"),
        (r'\binvest in\b', "Don't frame gambling as investing"),
        (r'\bfree money\b', "No pumpy gambling language"),
        (r'\binside info\b', "Do not claim secret insider information"),
        (r'\binsider source\b', "Do not claim secret sources"),
        (r'\bfixed\b', "Never imply match-fixing without sourced evidence"),
    ]
    
    # Corporate/AI tell phrases
    CORPORATE_TELLS = [
        'delve into', 'leverage', 'synergy', 'ecosystem',
        'disrupt', 'revolutionize', 'empower', 'transform',
        'excited to announce', 'thrilled to', 'proud to share',
        'dive deep', 'unpack', 'double-click', 'circle back'
    ]
    
    AI_TELLS = [
        'as an ai', 'i cannot', 'i apologize', 'certainly',
        'furthermore', 'moreover', 'additionally', 'in conclusion',
        'it is important to note', 'it should be noted'
    ]
    
    def __init__(self):
        self.client = get_openrouter_client()
        self.ml_model = None
        self._load_ml_model()
    
    def _load_ml_model(self):
        """Load pre-trained TF-IDF + SVM tone model from disk"""
        if not ML_AVAILABLE:
            return
        try:
            if self.MODEL_PATH.exists():
                with open(self.MODEL_PATH, 'rb') as f:
                    self.ml_model = _restricted_load(f)
                logger.info("✅ Loaded local tone classifier model")
        except Exception as e:
            logger.warning(f"⚠️  Could not load tone model: {e}")

    def train_local_model(self):
        """Train the local TF-IDF + SVM tone classifier from DB engagement data.

        Labels:
        - 'good': tweets marked 'posted' with engagement_rate in top 70%
        - 'bad': tweets marked 'rejected' / 'mirofish_veto' / 'expired' or bottom 10%
        """
        if not ML_AVAILABLE:
            logger.warning("⚠️  scikit-learn not available, cannot train tone model")
            return

        try:
            conn = psycopg2.connect(os.getenv('DATABASE_URL'))
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT content, status, engagement_rate
                    FROM twitter_bot.tweets_v2
                    WHERE content IS NOT NULL AND LENGTH(content) > 10
                    AND (status IN ('posted', 'mirofish_veto', 'rejected', 'expired')
                         OR engagement_rate IS NOT NULL)
                """)
                rows = cur.fetchall()
            conn.close()

            if len(rows) < self.MIN_TRAINING_SAMPLES:
                logger.warning(f"⚠️  Insufficient data ({len(rows)}/{self.MIN_TRAINING_SAMPLES})")
                return

            # Build labels
            posted = [(r[0], r[2]) for r in rows if r[1] == 'posted' and r[2] is not None]
            rejected = [r[0] for r in rows if r[1] in ('mirofish_veto', 'rejected', 'expired')]

            if not posted and not rejected:
                logger.warning("⚠️  No labeled data found")
                return

            # Top 70% of posted tweets by engagement → 'good'
            if posted:
                posted.sort(key=lambda x: x[1], reverse=True)
                cutoff = max(1, int(len(posted) * 0.7))
                good_texts = [t[0] for t in posted[:cutoff]]
                bad_from_posted = [t[0] for t in posted[cutoff:]]
            else:
                good_texts = []
                bad_from_posted = []

            texts = good_texts + bad_from_posted + rejected
            labels = (['good'] * len(good_texts) +
                      ['bad'] * len(bad_from_posted) +
                      ['bad'] * len(rejected))

            if len(set(labels)) < 2:
                logger.warning("⚠️  Need both good and bad samples to train")
                return

            pipeline = SkPipeline([
                ('tfidf', TfidfVectorizer(
                    max_features=3000, ngram_range=(1, 2),
                    stop_words='english', sublinear_tf=True)),
                ('svm', CalibratedClassifierCV(LinearSVC(max_iter=2000), cv=3)),
            ])
            pipeline.fit(texts, labels)

            self.MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(self.MODEL_PATH, 'wb') as f:
                pickle.dump(pipeline, f)
            self.ml_model = pipeline
            logger.info(f"✅ Trained tone classifier on {len(texts)} samples")

        except Exception as e:
            logger.error(f"❌ Tone model training failed: {e}")

    def ml_tone_check(self, text: str) -> Dict[str, Any]:
        """Score tone using local ML model. Returns None if model unavailable or low confidence."""
        if self.ml_model is None or not ML_AVAILABLE:
            return None
        try:
            proba = self.ml_model.predict_proba([text])[0]
            classes = list(self.ml_model.classes_)
            good_idx = classes.index('good') if 'good' in classes else 0
            confidence = float(proba[good_idx])

            if confidence < (1 - self.ML_CONFIDENCE_THRESHOLD) or confidence > self.ML_CONFIDENCE_THRESHOLD:
                # High-confidence prediction
                is_good = confidence > 0.5
                score = int(confidence * 10) if is_good else int((1 - confidence) * 10)
                return {
                    'valid': is_good,
                    'issues': [] if is_good else ['Local ML: tone does not match brand voice'],
                    'score': min(10, max(1, score)),
                    'method': 'ml_local',
                    'confidence': confidence,
                }
            # Low confidence → return None to escalate to LLM
            return None
        except Exception as e:
            logger.warning(f"⚠️  Local ML tone check failed: {e}")
            return None

    def check_forbidden_patterns(self, text: str) -> Dict[str, Any]:
        """Check for compliance violations"""
        violations = []
        
        for pattern, reason in self.FORBIDDEN_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                violations.append({
                    'pattern': pattern,
                    'reason': reason
                })
        
        return {
            'valid': len(violations) == 0,
            'violations': violations
        }
    
    def check_corporate_language(self, text: str) -> Dict[str, Any]:
        """Detect corporate/marketing speak"""
        text_lower = text.lower()
        detected = []
        
        for phrase in self.CORPORATE_TELLS:
            if phrase in text_lower:
                detected.append(phrase)
        
        return {
            'valid': len(detected) == 0,
            'detected': detected
        }
    
    def check_ai_tells(self, text: str) -> Dict[str, Any]:
        """Detect AI-generated language patterns"""
        text_lower = text.lower()
        detected = []
        
        for phrase in self.AI_TELLS:
            if phrase in text_lower:
                detected.append(phrase)
        
        return {
            'valid': len(detected) == 0,
            'detected': detected
        }
    
    def check_length(self, text: str) -> Dict[str, Any]:
        """Verify tweet length"""
        char_count = len(text)
        
        return {
            'valid': char_count <= 280,
            'char_count': char_count,
            'over_limit': max(0, char_count - 280)
        }
    
    def llm_tone_check(self, text: str) -> Dict[str, Any]:
        """Use LLM to evaluate tone quality"""
        system_prompt = """You are a tone validator for a sharp CS2 trader Twitter account.
    The voice should be: casual, sharp, short, like someone who watches the market and the games. Never corporate. Never formal.
Language level: B2 English. Simple words. Short sentences.

    Fail if: sounds like a journalist, sportsbook ad, fake insider, uses hard vocabulary, too many commas, em-dashes, or reads like a press release.

Respond with JSON:
{"passes": boolean, "issues": ["specific tone problems"], "score": 0-10}"""
        
        prompt = f"""Tweet draft:
"{text}"

Does this match our tone guidelines? (JSON only):"""
        
        try:
            result = self.client.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                tier='eco',
                temperature=0.2,
                max_tokens=150
            )
            
            import json
            response = json.loads(result['text'])
            
            return {
                'valid': response.get('passes', True),
                'issues': response.get('issues', []),
                'score': response.get('score', 7)
            }
            
        except Exception as e:
            logger.warning(f"⚠️  LLM tone check failed: {e}")
            return {'valid': True, 'issues': [], 'score': 7}  # Fail open
    
    def validate(self, text: str, pillar: int = None) -> Dict[str, Any]:
        """
        Run full tone validation pipeline.
        
        Stage 1: Rule-based checks (compliance, corporate, AI tells, length)
        Stage 2: ML local classifier (if trained and confident)
        Stage 3: LLM eco-tier fallback (if ML unavailable or uncertain)
        """
        logger.info("🎨 Running tone validation...")
        
        # Stage 1: Rule-based checks (always run)
        checks = {
            'compliance': self.check_forbidden_patterns(text),
            'corporate': self.check_corporate_language(text),
            'ai_tells': self.check_ai_tells(text),
            'length': self.check_length(text),
        }
        
        # Stage 2: Try local ML first, fall back to LLM
        ml_result = self.ml_tone_check(text)
        if ml_result is not None:
            checks['tone'] = ml_result
            logger.info(f"🤖 Local ML tone: score={ml_result['score']}/10 conf={ml_result['confidence']:.2f}")
        else:
            checks['tone'] = self.llm_tone_check(text)
            logger.info(f"🌐 LLM tone fallback: score={checks['tone']['score']}/10")
        
        # Separate critical issues from warnings
        critical_issues = []
        warnings = []
        
        # Compliance violations are critical
        if not checks['compliance']['valid']:
            critical_issues.extend([
                f"Compliance violation: {v['reason']}"
                for v in checks['compliance']['violations']
            ])
        
        # Length violations are critical
        if not checks['length']['valid']:
            critical_issues.append(
                f"Tweet too long: {checks['length']['char_count']}/280 chars"
            )
        
        # Corporate language is a warning
        if not checks['corporate']['valid']:
            warnings.extend([
                f"Corporate language detected: {phrase}"
                for phrase in checks['corporate']['detected']
            ])
        
        # AI tells are a warning
        if not checks['ai_tells']['valid']:
            warnings.extend([
                f"AI tell detected: {phrase}"
                for phrase in checks['ai_tells']['detected']
            ])
        
        # Tone issues are warnings
        if not checks['tone']['valid']:
            warnings.extend(checks['tone']['issues'])
        
        valid = len(critical_issues) == 0
        tone_score = checks['tone']['score']
        tone_method = checks['tone'].get('method', 'llm')
        
        logger.info(f"🎨 Tone validation complete: {'✅ Valid' if valid else '❌ Issues'} (Score: {tone_score}/10, via {tone_method})")
        
        return {
            'valid': valid,
            'issues': critical_issues,
            'warnings': warnings,
            'tone_score': tone_score,
            'char_count': checks['length']['char_count'],
            'checks_run': list(checks.keys())
        }


# Singleton instance
_validator = None

def get_tone_validator() -> ToneValidator:
    """Get or create tone validator singleton"""
    global _validator
    if _validator is None:
        _validator = ToneValidator()
    return _validator


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Tone Validator')
    parser.add_argument('--train', action='store_true', help='Train local ML tone model from DB data')
    parser.add_argument('--test', type=str, help='Validate a specific tweet text')
    args = parser.parse_args()

    validator = get_tone_validator()

    if args.train:
        validator.train_local_model()
    elif args.test:
        result = validator.validate(args.test)
        print(f"Valid: {result['valid']}")
        print(f"Score: {result['tone_score']}/10")
        if result['issues']:
            print(f"Issues: {result['issues']}")
        if result['warnings']:
            print(f"Warnings: {result['warnings'][:3]}")
    else:
        test_tweets = [
            "NaVi 2-0 G2. s1mple casually dropping 40 frags. Water is wet.",
            "We're excited to announce that we're leveraging our ecosystem to empower users!",
            "This is a guaranteed win. Can't lose. Risk-free betting!",
            "ZywOo will definitely win MVP. Absolutely certain on this one.",
        ]
        for tweet in test_tweets:
            print(f"\n🧪 Testing: {tweet}")
            result = validator.validate(tweet)
            print(f"   Valid: {result['valid']}")
            print(f"   Score: {result['tone_score']}/10")
            if result['issues']:
                print(f"   Issues: {result['issues']}")
            if result['warnings']:
                print(f"   Warnings: {result['warnings'][:2]}")
