#!/usr/bin/env python3
"""
Persona Classifier - Twitter Bot Pipeline V2
ML-based persona selection trained on RLHF engagement data.
Maps {news_category, time_of_day, topic_sentiment, urgency} → best_persona.

Trains weekly from rlhf_history + tweets_v2 engagement data.
Falls back to rule-based selection when model isn't trained yet.
"""

import logging
import os
import pickle
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from dotenv import load_dotenv
import numpy as np
import psycopg2

# ML: lightweight sklearn classifier (no GPU needed)
try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import LabelEncoder, StandardScaler
    from sklearn.pipeline import Pipeline
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    logging.warning("⚠️  scikit-learn not available. Persona classifier uses rule-based fallback.")


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


# Load environment
load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Persona archetypes (aligned with MiroFish guard personas)
PERSONAS = {
    'degen_analyst': 'Sharp desk energy. Reads price, momentum, and overreaction before casuals do.',
    'news_breaker': 'Fast tape-reader. Posts the news and what it changes for the market.',
    'hot_take_artist': 'Aggressive market opinion. Fade the public. Slightly provocative.',
    'meme_lord': 'Internet culture native with trading-desk humor and self-aware edge.',
    'data_nerd': 'Model-first trader. Numbers, deltas, and clean evidence.',
    'drama_commentator': 'Roster chaos and org drama through a market lens.',
    'odds_shark': 'Closest thing to the sharp side of CS2 Twitter. Spots steam, bad numbers, and overreaction.',
    'community_engager': 'Reply mode. Sharp, grounded, and never salesy.',
}

# Rule-based fallback mapping: category → persona
CATEGORY_PERSONA_MAP = {
    'roster_change': 'news_breaker',
    'match_result': 'odds_shark',
    'regulation': 'drama_commentator',
    'drama': 'drama_commentator',
    'odds_movement': 'odds_shark',
    'match_prediction': 'odds_shark',
    'financial': 'data_nerd',
    'meme': 'meme_lord',
    'cs2_update': 'news_breaker',
    'engagement_take': 'hot_take_artist',
    'engagement_recycle': 'hot_take_artist',
    'engagement_conversation': 'community_engager',
    'community_disagreement': 'community_engager',
    'general': 'degen_analyst',
}


class PersonaClassifier:
    """ML persona selector trained on engagement data"""

    def __init__(self):
        self.db_conn = None
        self.model = None
        self.label_encoder = LabelEncoder() if ML_AVAILABLE else None
        self.category_encoder = LabelEncoder() if ML_AVAILABLE else None
        self.model_path = Path(__file__).parent.parent.parent / 'models' / 'persona_classifier.pkl'

        # Try to load pre-trained model
        self._load_model()

    def connect_db(self):
        try:
            self.db_conn = psycopg2.connect(os.getenv('DATABASE_URL'))
            logger.info("✅ Connected to PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise

    def _load_model(self):
        """Load pre-trained model from disk if available"""
        if not ML_AVAILABLE:
            return
        try:
            if self.model_path.exists():
                with open(self.model_path, 'rb') as f:
                    saved = _restricted_load(f)
                self.model = saved['pipeline']
                self.label_encoder = saved['label_encoder']
                self.category_encoder = saved['category_encoder']
                logger.info("✅ Loaded persona classifier model")
        except Exception as e:
            logger.warning(f"⚠️  Could not load persona model: {e}")

    def _save_model(self):
        """Persist trained model to disk"""
        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.model_path, 'wb') as f:
                pickle.dump({
                    'pipeline': self.model,
                    'label_encoder': self.label_encoder,
                    'category_encoder': self.category_encoder,
                    'trained_at': datetime.now(timezone.utc).isoformat(),
                }, f)
            logger.info(f"✅ Saved persona classifier to {self.model_path}")
        except Exception as e:
            logger.error(f"❌ Failed to save model: {e}")

    def _extract_features(self, category: str, hour: int, urgency: str) -> np.ndarray:
        """Convert event attributes to feature vector.

        Features:
        [0] category_encoded (int)
        [1] hour_sin (cyclical encoding)
        [2] hour_cos (cyclical encoding)
        [3] urgency_score (breaking=4, important=3, normal=2, skip=1)
        """
        known_cats = list(self.category_encoder.classes_) if self.category_encoder and hasattr(self.category_encoder, 'classes_') else []
        cat_val = self.category_encoder.transform([category])[0] if category in known_cats else 0

        urgency_map = {'breaking': 4, 'important': 3, 'normal': 2, 'skip': 1}
        urg_score = urgency_map.get(urgency, 2)

        hour_sin = np.sin(2 * np.pi * hour / 24)
        hour_cos = np.cos(2 * np.pi * hour / 24)

        return np.array([[cat_val, hour_sin, hour_cos, urg_score]], dtype=np.float64)

    def train(self):
        """Train the classifier on historical engagement data.

        Uses tweets_v2 engagement_rate grouped by category + pillar + hour
        to learn which persona (mapped from pillar) gets best engagement.
        """
        if not ML_AVAILABLE:
            logger.warning("⚠️  scikit-learn not available, skipping training")
            return

        self.connect_db()

        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT e.category, t.pillar, 
                           EXTRACT(HOUR FROM t.posted_at) as hour,
                           e.urgency,
                           t.engagement_rate
                    FROM twitter_bot.tweets_v2 t
                    JOIN twitter_bot.events e ON t.event_id = e.id
                    WHERE t.status = 'posted'
                    AND t.engagement_rate IS NOT NULL
                    AND t.impressions > 50
                    AND t.posted_at > NOW() - INTERVAL '90 days'
                """)
                rows = cur.fetchall()

            if len(rows) < 30:
                logger.warning(f"⚠️  Insufficient training data ({len(rows)} rows), need 30+")
                return

            # Map pillar to persona label
            pillar_persona = {
                1: 'news_breaker', 2: 'degen_analyst', 3: 'hot_take_artist',
                4: 'data_nerd', 5: 'meme_lord', 7: 'drama_commentator',
                9: 'odds_shark', 10: 'community_engager', 11: 'hot_take_artist',
                12: 'community_engager',
            }

            # For each (category, hour, urgency), pick the persona with highest avg engagement
            from collections import defaultdict
            buckets = defaultdict(lambda: defaultdict(list))
            categories_seen = set()

            for cat, pillar, hour, urgency, er in rows:
                cat = cat or 'general'
                categories_seen.add(cat)
                persona = pillar_persona.get(pillar, 'hot_take_artist')
                key = (cat, int(hour or 12), urgency or 'normal')
                buckets[key][persona].append(float(er))

            # Build training set: each bucket → best persona
            X_raw, y_raw = [], []
            for (cat, hour, urgency), persona_ers in buckets.items():
                best_persona = max(persona_ers.keys(), key=lambda p: np.mean(persona_ers[p]))
                X_raw.append((cat, hour, urgency))
                y_raw.append(best_persona)

            if len(X_raw) < 10:
                logger.warning(f"⚠️  Not enough distinct buckets ({len(X_raw)}), need 10+")
                return

            # Encode
            self.category_encoder.fit(list(categories_seen | {'general'}))
            self.label_encoder.fit(list(PERSONAS.keys()))

            X = np.vstack([
                self._extract_features(cat, hour, urg) for cat, hour, urg in X_raw
            ])
            y = self.label_encoder.transform(y_raw)

            # Train logistic regression pipeline
            self.model = Pipeline([
                ('scaler', StandardScaler()),
                ('clf', LogisticRegression(max_iter=1000, multi_class='multinomial'))
            ])
            self.model.fit(X, y)

            self._save_model()
            logger.info(f"✅ Persona classifier trained on {len(X)} samples")

        except Exception as e:
            logger.error(f"❌ Training failed: {e}")
        finally:
            if self.db_conn:
                self.db_conn.close()
                self.db_conn = None

    def classify(self, category: str, urgency: str = 'normal', hour: Optional[int] = None) -> Dict[str, Any]:
        """Classify the best persona for a given event context.

        Returns:
            Dict with 'persona', 'description', 'confidence', 'method'
        """
        if hour is None:
            hour = datetime.now(timezone.utc).hour

        # Try ML model first
        if self.model is not None and ML_AVAILABLE:
            try:
                features = self._extract_features(category, hour, urgency)
                proba = self.model.predict_proba(features)[0]
                pred_idx = np.argmax(proba)
                persona = self.label_encoder.inverse_transform([pred_idx])[0]
                confidence = float(proba[pred_idx])

                return {
                    'persona': persona,
                    'description': PERSONAS.get(persona, ''),
                    'confidence': confidence,
                    'method': 'ml_classifier',
                }
            except Exception as e:
                logger.warning(f"⚠️  ML classification failed, using fallback: {e}")

        # Rule-based fallback
        persona = CATEGORY_PERSONA_MAP.get(category, 'hot_take_artist')
        return {
            'persona': persona,
            'description': PERSONAS.get(persona, ''),
            'confidence': 0.6,
            'method': 'rule_based',
        }


# Singleton
_classifier = None

def get_persona_classifier() -> PersonaClassifier:
    global _classifier
    if _classifier is None:
        _classifier = PersonaClassifier()
    return _classifier


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Persona Classifier')
    parser.add_argument('--train', action='store_true', help='Train model from DB engagement data')
    parser.add_argument('--classify', type=str, help='Classify persona for a category')
    args = parser.parse_args()

    clf = get_persona_classifier()

    if args.train:
        clf.train()
    elif args.classify:
        result = clf.classify(category=args.classify)
        print(f"Category: {args.classify}")
        print(f"Persona:  {result['persona']} ({result['method']})")
        print(f"Desc:     {result['description']}")
        print(f"Conf:     {result['confidence']:.2f}")
    else:
        # Demo
        for cat in ['roster_change', 'match_result', 'regulation', 'odds_movement', 'meme']:
            result = clf.classify(category=cat)
            print(f"  {cat:20s} → {result['persona']:20s} ({result['method']}, {result['confidence']:.2f})")
