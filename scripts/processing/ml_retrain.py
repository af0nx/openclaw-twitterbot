#!/usr/bin/env python3
"""
ML Retrainer - Runs all learning systems in sequence.
Triggered by PM2 cron (weekly) or manually.

1. RLHF Tuner — updates system_prompt_appendix.txt based on engagement data
2. Persona Classifier — retrains best-persona-per-category from engagement
3. Tone Validator — retrains approved/rejected tweet patterns
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv('/dev/shm/.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def run_rlhf():
    logger.info("=" * 50)
    logger.info("🧠 [1/3] Running RLHF Tuner...")
    try:
        from processing.rlhf_tuner import RLHFTuner
        tuner = RLHFTuner()
        tuner.run_weekly_tune()
        logger.info("✅ RLHF Tuner complete")
        return True
    except Exception as e:
        logger.error(f"❌ RLHF Tuner failed: {e}")
        return False


def run_persona_training():
    logger.info("=" * 50)
    logger.info("🎭 [2/3] Training Persona Classifier...")
    try:
        from processing.persona_classifier import PersonaClassifier
        classifier = PersonaClassifier()
        classifier.train()
        logger.info("✅ Persona Classifier trained")
        return True
    except Exception as e:
        logger.error(f"❌ Persona Classifier training failed: {e}")
        return False


def run_tone_training():
    logger.info("=" * 50)
    logger.info("🎨 [3/3] Training Tone Validator...")
    try:
        from processing.tone_validator import ToneValidator
        validator = ToneValidator()
        validator.train_local_model()
        logger.info("✅ Tone Validator trained")
        return True
    except Exception as e:
        logger.error(f"❌ Tone Validator training failed: {e}")
        return False


def main():
    logger.info("🚀 ML Retrainer starting — all learning systems")
    results = [
        run_rlhf(),
        run_persona_training(),
        run_tone_training(),
    ]
    logger.info("=" * 50)
    if all(results):
        logger.info("✅ All ML retraining complete")
    else:
        failed = sum(1 for r in results if not r)
        logger.error(f"⚠️  ML retraining finished with {failed}/3 failures")
        sys.exit(1)


if __name__ == '__main__':
    main()
