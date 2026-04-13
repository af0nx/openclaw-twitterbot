#!/usr/bin/env python3
"""
RLHF Tuner - Twitter Bot Pipeline V2
Weekly auto-learning system that updates system prompt based on engagement
Runs every Sunday at 23:00 UTC
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
import os
from pathlib import Path

from dotenv import load_dotenv
import psycopg2
import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.openrouter_client import get_openrouter_client

# Sentence-transformers for real cosine similarity (drift ceiling)
try:
    from sentence_transformers import SentenceTransformer
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False
    logging.warning("⚠️  sentence-transformers not available. Drift ceiling uses fallback Jaccard.")

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class RLHFTuner:
    """Auto-learning system for persona refinement"""
    
    def __init__(self):
        self.client = get_openrouter_client()
        self.db_conn = None
        self.enabled = os.getenv('ENABLE_AUTO_RLHF', 'true').lower() == 'true'
        
        self.config_dir = Path(__file__).parent.parent.parent / 'config'
        self.appendix_file = self.config_dir / 'system_prompt_appendix.txt'
        self.original_appendix_file = self.config_dir / 'system_prompt_appendix_week0.txt'
        
        # Drift ceiling parameters
        self.WEEKLY_DRIFT_THRESHOLD = 0.70  # vs previous week
        self.CUMULATIVE_DRIFT_THRESHOLD = 0.30  # vs Week 0 (relaxed — LLM outputs vary widely)
        self.MAX_APPENDIX_TOKENS = 200
        
        # Load sentence-transformer model for drift ceiling
        self.embedding_model = None
        if EMBEDDINGS_AVAILABLE:
            try:
                self.embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
                logger.info("✅ Loaded sentence-transformer model for drift ceiling")
            except Exception as e:
                logger.warning(f"⚠️  Failed to load embedding model: {e}")
        
        if not self.enabled:
            logger.warning("⚠️  Auto-RLHF is DISABLED via env config")
    
    def connect_db(self):
        """Establish PostgreSQL connection (reuses or reconnects)"""
        try:
            if self.db_conn:
                try:
                    self.db_conn.close()
                except Exception:
                    pass
            self.db_conn = psycopg2.connect(os.getenv('DATABASE_URL'))
            logger.info("✅ Connected to PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise
    
    def get_weekly_performance(self) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch top and bottom performing tweets from the past week"""
        try:
            with self.db_conn.cursor() as cur:
                # Recalculate engagement rate (same formula as engagement_tracker)
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET engagement_rate = 
                        CASE 
                            WHEN impressions > 0 
                            THEN (likes + replies + retweets + quotes + bookmarks)::float / impressions * 100
                            ELSE 0
                        END
                    WHERE posted_at > NOW() - INTERVAL '7 days'
                    AND engagement_rate IS NULL
                """)
                
                # Get top 5
                cur.execute("""
                    SELECT id, content, pillar, engagement_rate, likes, replies, impressions
                    FROM twitter_bot.tweets_v2
                    WHERE posted_at > NOW() - INTERVAL '7 days'
                    AND status = 'posted'
                    AND impressions > 5  -- Minimum threshold
                    ORDER BY engagement_rate DESC NULLS LAST
                    LIMIT 5
                """)
                
                top_tweets = []
                for row in cur.fetchall():
                    top_tweets.append({
                        'id': str(row[0]),
                        'content': row[1],
                        'pillar': row[2],
                        'engagement_rate': float(row[3]) if row[3] else 0,
                        'likes': row[4],
                        'replies': row[5],
                        'impressions': row[6]
                    })
                
                # Get bottom 5
                cur.execute("""
                    SELECT id, content, pillar, engagement_rate, likes, replies, impressions
                    FROM twitter_bot.tweets_v2
                    WHERE posted_at > NOW() - INTERVAL '7 days'
                    AND status = 'posted'
                    AND impressions > 5
                    ORDER BY engagement_rate ASC NULLS LAST
                    LIMIT 5
                """)
                
                bottom_tweets = []
                for row in cur.fetchall():
                    bottom_tweets.append({
                        'id': str(row[0]),
                        'content': row[1],
                        'pillar': row[2],
                        'engagement_rate': float(row[3]) if row[3] else 0,
                        'likes': row[4],
                        'replies': row[5],
                        'impressions': row[6]
                    })
                
                # Mark tweets for RLHF tracking
                all_ids = [t['id'] for t in top_tweets + bottom_tweets]
                if all_ids:
                    cur.execute("""
                        UPDATE twitter_bot.tweets_v2
                        SET rlhf_classified = CASE
                            WHEN id = ANY(%s::uuid[]) THEN 'top_5'
                            WHEN id = ANY(%s::uuid[]) THEN 'bottom_5'
                            ELSE rlhf_classified
                        END
                        WHERE id = ANY(%s::uuid[])
                    """, ([t['id'] for t in top_tweets], [t['id'] for t in bottom_tweets], all_ids))
                
                self.db_conn.commit()
                
                logger.info(f"📊 Fetched {len(top_tweets)} top and {len(bottom_tweets)} bottom performers")
                
                return {
                    'top': top_tweets,
                    'bottom': bottom_tweets
                }
                
        except Exception as e:
            logger.error(f"❌ Failed to fetch weekly performance: {e}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass
            return {'top': [], 'bottom': []}
    
    def analyze_performance_delta(self, top_tweets: List[Dict], bottom_tweets: List[Dict]) -> str:
        """Use LLM to analyze what makes top tweets succeed"""
        system_prompt = """You are an AI trainer analyzing tweet performance.
Extract actionable insights about what makes successful tweets work.
Focus on: tone, length, structure, data usage, humor style.
Output 2-3 brief rules (max 200 tokens total)."""
        
        top_examples = "\n".join([f"- {t['content']} (ER: {t['engagement_rate']:.3f})" for t in top_tweets])
        bottom_examples = "\n".join([f"- {t['content']} (ER: {t['engagement_rate']:.3f})" for t in bottom_tweets])
        
        prompt = f"""TOP PERFORMING TWEETS (Week):
{top_examples}

BOTTOM PERFORMING TWEETS (Week):
{bottom_examples}

What linguistic/structural patterns differentiate winners from losers?
Provide 2-3 actionable rules (be specific, concise):"""
        
        result = self.client.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            tier='auto',
            temperature=0.3,
            max_tokens=self.MAX_APPENDIX_TOKENS
        )
        
        return result['text'].strip()
    
    def calculate_similarity(self, text1: str, text2: str) -> float:
        """Calculate cosine similarity between two texts using sentence-transformers embeddings"""
        try:
            if self.embedding_model is not None:
                embeddings = self.embedding_model.encode([text1, text2], convert_to_numpy=True)
                norm_a = np.linalg.norm(embeddings[0])
                norm_b = np.linalg.norm(embeddings[1])
                if norm_a == 0 or norm_b == 0:
                    return 0.0
                cosine_sim = float(np.dot(embeddings[0], embeddings[1]) / (norm_a * norm_b))
                return max(0.0, min(1.0, cosine_sim))
            
            # Fallback: Jaccard word overlap (only if sentence-transformers unavailable)
            logger.warning("⚠️  Using Jaccard fallback — install sentence-transformers for accurate drift ceiling")
            words1 = set(text1.lower().split())
            words2 = set(text2.lower().split())
            intersection = len(words1 & words2)
            union = len(words1 | words2)
            return intersection / union if union > 0 else 0.0
            
        except Exception as e:
            logger.error(f"❌ Similarity calculation failed: {e}", exc_info=True)
            return -1.0  # Negative = error. Caller must handle.
    
    def enforce_drift_ceiling(
        self,
        new_appendix: str,
        old_appendix: str,
        original_appendix: str
    ) -> Dict[str, Any]:
        """Check if new appendix violates drift thresholds"""
        
        # Weekly drift (vs previous week)
        weekly_similarity = self.calculate_similarity(new_appendix, old_appendix)
        
        # Cumulative drift (vs Week 0)
        cumulative_similarity = self.calculate_similarity(new_appendix, original_appendix)
        
        reject = False
        rejection_reason = None
        
        # If similarity calculation errored, reject with clear reason
        if weekly_similarity < 0 or cumulative_similarity < 0:
            reject = True
            rejection_reason = f"Similarity calculation failed (weekly={weekly_similarity:.2f}, cumul={cumulative_similarity:.2f})"
        elif weekly_similarity < self.WEEKLY_DRIFT_THRESHOLD:
            reject = True
            rejection_reason = f"Weekly drift too high (similarity: {weekly_similarity:.2f} < {self.WEEKLY_DRIFT_THRESHOLD})"
        elif cumulative_similarity < self.CUMULATIVE_DRIFT_THRESHOLD:
            reject = True
            rejection_reason = f"Cumulative drift too high (similarity: {cumulative_similarity:.2f} < {self.CUMULATIVE_DRIFT_THRESHOLD})"
        
        logger.info(f"📐 Drift check: Weekly={weekly_similarity:.2f}, Cumulative={cumulative_similarity:.2f}")
        logger.info(f"📐 Thresholds: Weekly>{self.WEEKLY_DRIFT_THRESHOLD}, Cumulative>{self.CUMULATIVE_DRIFT_THRESHOLD}")
        
        return {
            'reject': reject,
            'rejection_reason': rejection_reason,
            'weekly_similarity': weekly_similarity,
            'cumulative_similarity': cumulative_similarity
        }
    
    def apply_update(self, new_appendix: str, analysis_summary: str):
        """Write new appendix to disk (atomic write to avoid partial reads)"""
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            
            # Atomic write: write to temp file then rename
            tmp_path = self.appendix_file.with_suffix('.tmp')
            with open(tmp_path, 'w') as f:
                f.write(new_appendix)
            tmp_path.rename(self.appendix_file)
            
            logger.info(f"✅ Applied new RLHF appendix ({len(new_appendix)} chars)")
            
        except Exception as e:
            logger.error(f"❌ Failed to write appendix: {e}")
            raise
    
    def store_history(
        self,
        top_tweets: List[Dict],
        bottom_tweets: List[Dict],
        analysis_summary: str,
        old_appendix: str,
        new_appendix: str,
        drift_check: Dict[str, Any]
    ):
        """Store RLHF history in database"""
        try:
            with self.db_conn.cursor() as cur:
                top_ids = [t['id'] for t in top_tweets]
                bottom_ids = [t['id'] for t in bottom_tweets]
                cur.execute("""
                    INSERT INTO twitter_bot.rlhf_history
                    (week_start, top_tweets, bottom_tweets, analysis_summary,
                     old_appendix, new_appendix, similarity_score, cumulative_similarity,
                     applied, rejection_reason)
                    VALUES (%s, %s::uuid[], %s::uuid[], %s, %s, %s, %s, %s, %s, %s)
                """, (
                    (datetime.now(timezone.utc) - timedelta(days=7)).date(),
                    top_ids,
                    bottom_ids,
                    analysis_summary,
                    old_appendix,
                    new_appendix,
                    drift_check['weekly_similarity'],
                    drift_check['cumulative_similarity'],
                    not drift_check['reject'],
                    drift_check.get('rejection_reason')
                ))
                
                self.db_conn.commit()
                logger.info("📝 Stored RLHF history")
                
        except Exception as e:
            logger.error(f"❌ Failed to store history: {e}")
            try:
                self.db_conn.rollback()
            except Exception:
                pass
    
    def run_weekly_tune(self):
        """Run the full weekly RLHF tuning cycle"""
        if not self.enabled:
            logger.info("ℹ️  Auto-RLHF disabled, skipping")
            return
        
        logger.info("🔄 Starting weekly RLHF tuning...")
        
        self.connect_db()
        
        # Fetch performance data
        performance = self.get_weekly_performance()
        
        if not performance['top'] or not performance['bottom']:
            logger.warning("⚠️  Insufficient data for RLHF tuning")
            return
        
        # Analyze performance delta
        analysis = self.analyze_performance_delta(
            performance['top'],
            performance['bottom']
        )
        
        logger.info(f"📊 Analysis: {analysis[:100]}...")
        
        # Load current appendix
        old_appendix = ""
        if self.appendix_file.exists():
            with open(self.appendix_file) as f:
                old_appendix = f.read().strip()
        
        # Load original Week 0 appendix
        original_appendix = old_appendix
        if self.original_appendix_file.exists():
            with open(self.original_appendix_file) as f:
                original_appendix = f.read().strip()
            logger.info(f"📐 Week0 baseline loaded ({len(original_appendix)} chars): {original_appendix[:100]}...")
        else:
            logger.info("📐 No Week0 baseline file found — will be created this run")
        
        new_appendix = analysis
        
        # First run: skip drift check (nothing to compare against)
        if not old_appendix:
            logger.info("ℹ️  First RLHF run — no existing appendix, skipping drift check")
            drift_check = {'reject': False, 'rejection_reason': None, 'weekly_similarity': 1.0, 'cumulative_similarity': 1.0}
            self.apply_update(new_appendix, analysis)
            # Save first actual appendix as Week 0 baseline (not empty)
            if not self.original_appendix_file.exists() or self.original_appendix_file.stat().st_size == 0:
                with open(self.original_appendix_file, 'w') as f:
                    f.write(new_appendix)
        else:
            # Enforce drift ceiling
            drift_check = self.enforce_drift_ceiling(
                new_appendix,
                old_appendix,
                original_appendix
            )
            
            if drift_check['reject']:
                logger.warning(f"❌ RLHF update REJECTED: {drift_check['rejection_reason']}")
            else:
                logger.info("✅ RLHF update APPROVED, applying...")
                self.apply_update(new_appendix, analysis)
        
        # Store history
        self.store_history(
            performance['top'],
            performance['bottom'],
            analysis,
            old_appendix,
            new_appendix,
            drift_check
        )
        
        logger.info("✅ Weekly RLHF tuning complete")


if __name__ == '__main__':
    tuner = RLHFTuner()
    tuner.run_weekly_tune()
