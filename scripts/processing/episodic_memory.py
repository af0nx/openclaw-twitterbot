#!/usr/bin/env python3
"""
Episodic Memory - Twitter Bot Pipeline V2
pgvector-backed semantic recall of past VIP interactions.

Used by content_generator to inject relevant context when crafting
VIP replies (Pillar 12) — e.g., "You mentioned X last week..."

Storage: siftly_events.embedding VECTOR(768) + events table
Query: pgvector cosine similarity search over recent VIP interactions
"""

import logging
import os
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv
import psycopg2

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db_utils import ensure_db_connection

# Sentence-transformers for query embedding
try:
    from sentence_transformers import SentenceTransformer
    import numpy as np
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False
    logging.warning("⚠️  sentence-transformers not available. Episodic memory disabled.")

load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _get_env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)


def _get_bool_env(key: str, default: bool = False) -> bool:
    value = _get_env(key)
    if value in (None, ''):
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


class EpisodicMemory:
    """Semantic recall engine for VIP interaction context"""

    def __init__(self):
        self.db_conn = None
        self.embedding_model = None
        self.MAX_RECALL_ITEMS = 5
        self.RECALL_WINDOW_DAYS = 30
        self.enabled = _get_bool_env('EPISODIC_MEMORY_ENABLED', True)
        self.model_name = _get_env('SENTENCE_TRANSFORMERS_MODEL', 'all-MiniLM-L6-v2')
        self.local_files_only = _get_bool_env('SENTENCE_TRANSFORMERS_LOCAL_ONLY', False)

        if not self.enabled:
            logger.info("ℹ️  Episodic memory disabled by EPISODIC_MEMORY_ENABLED")
            return

        if EMBEDDINGS_AVAILABLE:
            try:
                self.embedding_model = SentenceTransformer(
                    self.model_name,
                    local_files_only=self.local_files_only,
                )
                logger.info("✅ Episodic memory embedding model loaded")
            except Exception as e:
                logger.warning(
                    "⚠️  Failed to load episodic memory embedding model "
                    f"'{self.model_name}' (local_only={self.local_files_only}); "
                    f"continuing without semantic recall: {e}"
                )

    def connect_db(self):
        try:
            self.db_conn = ensure_db_connection(self.db_conn)
            logger.info("✅ Connected to PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise

    def _ensure_connected(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    def _encode_query(self, text: str) -> Optional[list]:
        """Encode query text to 768-dim vector (384 padded to 768)"""
        if not self.embedding_model or not text.strip():
            return None
        try:
            vec = self.embedding_model.encode(text, convert_to_numpy=True)
            padded = np.zeros(768, dtype=np.float32)
            padded[:len(vec)] = vec
            return padded.tolist()
        except Exception as e:
            logger.error(f"❌ Query encoding failed: {e}")
            return None

    def recall_vip_context(self, vip_username: str, current_topic: str) -> List[Dict[str, Any]]:
        """Retrieve past interactions with a VIP relevant to the current topic.

        Uses pgvector cosine similarity over siftly_events embeddings
        filtered by vip_author. Returns top-k most relevant past items.

        Args:
            vip_username: Twitter handle of the VIP
            current_topic: Current event headline/content for semantic matching

        Returns:
            List of dicts with 'text', 'timestamp', 'similarity', 'semantic_tags'
        """
        if not EMBEDDINGS_AVAILABLE or not self.embedding_model:
            return []

        self._ensure_connected()

        query_embedding = self._encode_query(current_topic)
        if query_embedding is None:
            return []

        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        se.raw_text,
                        se.ocr_extracted_text,
                        se.semantic_tags,
                        se.created_at,
                        1 - (se.embedding <=> %s::vector) AS similarity
                    FROM twitter_bot.siftly_events se
                    WHERE se.vip_author = %s
                    AND se.embedding IS NOT NULL
                    AND se.created_at > NOW() - INTERVAL '%s days'
                    ORDER BY se.embedding <=> %s::vector ASC
                    LIMIT %s
                """, (
                    str(query_embedding),
                    vip_username,
                    self.RECALL_WINDOW_DAYS,
                    str(query_embedding),
                    self.MAX_RECALL_ITEMS,
                ))

                results = []
                for row in cur.fetchall():
                    raw_text, ocr_text, tags, created_at, similarity = row
                    text = raw_text or ocr_text or ''
                    if not text.strip():
                        continue
                    results.append({
                        'text': text[:500],  # Truncate for context window
                        'timestamp': created_at.isoformat() if created_at else None,
                        'similarity': round(float(similarity), 4) if similarity else 0.0,
                        'semantic_tags': tags or [],
                    })

                logger.info(f"📚 Recalled {len(results)} episodic memories for @{vip_username}")
                return results

        except Exception as e:
            logger.error(f"❌ Episodic recall failed for @{vip_username}: {e}")
            return []

    def recall_topic_context(self, topic: str) -> List[Dict[str, Any]]:
        """Retrieve recent events semantically similar to a topic (any VIP).

        Useful for Pillar 3 (hot takes) and Pillar 10 (daily threads)
        to avoid repeating recent content and to reference ongoing narratives.
        """
        if not EMBEDDINGS_AVAILABLE or not self.embedding_model:
            return []

        self._ensure_connected()

        query_embedding = self._encode_query(topic)
        if query_embedding is None:
            return []

        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        e.headline,
                        e.category,
                        e.source,
                        e.created_at,
                        1 - (se.embedding <=> %s::vector) AS similarity
                    FROM twitter_bot.siftly_events se
                    JOIN twitter_bot.events e ON e.siftly_event_id = se.id
                    WHERE se.embedding IS NOT NULL
                    AND e.category NOT IN ('match_prediction')
                    AND se.created_at > NOW() - INTERVAL '%s days'
                    AND e.created_at > NOW() - INTERVAL '%s days'
                    ORDER BY se.embedding <=> %s::vector ASC
                    LIMIT %s
                """, (
                    str(query_embedding),
                    self.RECALL_WINDOW_DAYS,
                    self.RECALL_WINDOW_DAYS,
                    str(query_embedding),
                    self.MAX_RECALL_ITEMS,
                ))

                results = []
                for row in cur.fetchall():
                    headline, category, source, created_at, similarity = row
                    results.append({
                        'headline': headline,
                        'category': category,
                        'source': source,
                        'timestamp': created_at.isoformat() if created_at else None,
                        'similarity': round(float(similarity), 4) if similarity else 0.0,
                    })

                logger.info(f"📚 Recalled {len(results)} topic memories for '{topic[:60]}...'")
                return results

        except Exception as e:
            logger.error(f"❌ Topic recall failed: {e}")
            return []

    def format_for_prompt(self, memories: List[Dict[str, Any]]) -> str:
        """Format recalled memories as context for LLM prompt injection.

        Returns a concise string block suitable for system prompt injection.
        """
        if not memories:
            return ""

        lines = ["EPISODIC MEMORY (past interactions):"]
        for i, mem in enumerate(memories[:3], 1):
            text = mem.get('text') or mem.get('headline', '')
            ts = mem.get('timestamp', 'unknown')[:10]  # Date only
            sim = mem.get('similarity', 0)
            lines.append(f"  [{i}] ({ts}, rel={sim:.2f}) {text[:120]}")

        return "\n".join(lines)

    def cleanup(self):
        if self.db_conn:
            self.db_conn.close()
            self.db_conn = None


# Singleton
_memory = None

def get_episodic_memory() -> EpisodicMemory:
    global _memory
    if _memory is None:
        _memory = EpisodicMemory()
    return _memory


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Episodic Memory')
    parser.add_argument('--vip', type=str, help='VIP username to recall context for')
    parser.add_argument('--topic', type=str, required=True, help='Topic/headline to search for')
    args = parser.parse_args()

    mem = get_episodic_memory()
    mem.connect_db()

    if args.vip:
        results = mem.recall_vip_context(args.vip, args.topic)
    else:
        results = mem.recall_topic_context(args.topic)

    prompt_ctx = mem.format_for_prompt(results)
    print(prompt_ctx if prompt_ctx else "No episodic memories found.")
    mem.cleanup()
