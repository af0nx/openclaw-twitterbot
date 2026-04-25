#!/usr/bin/env python3
"""
Siftly Engine - Twitter Bot Pipeline V2
Vision analysis and OCR for images in VIP tweets
Extracts data from charts, screenshots, and meme formats
"""

import argparse
import asyncio
import gc
import json
import logging
import re
from typing import List, Dict, Any, Optional
import os
from pathlib import Path

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
from PIL import Image
import requests
import numpy as np
from io import BytesIO

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from processing.openrouter_client import get_openrouter_client
from utils.db_utils import ensure_db_connection

# BLIP vision model: DISABLED — costs ~1.5GB RAM for <1 event/day.
# The tweet_scheduler already uses Gemini Flash for superior vision analysis.
# Re-enable by setting SIFTLY_ENABLE_BLIP=1 in .env if needed.
VISION_MODEL_AVAILABLE = False
if os.getenv('SIFTLY_ENABLE_BLIP', '0') == '1':
    try:
        import torch
        from transformers import BlipProcessor, BlipForConditionalGeneration
        VISION_MODEL_AVAILABLE = True
    except ImportError:
        logging.warning("⚠️  Vision models not available. Install torch+transformers for full functionality.")

# Sentence-transformers for semantic embeddings: DISABLED in Siftly.
# Importing sentence-transformers pulls in PyTorch (~500MB).
# With ~1 event/day, simple text dedup is sufficient.
# Other services (content_generator, episodic_memory) handle embeddings separately.
EMBEDDINGS_AVAILABLE = False

# Optional: pytesseract for OCR
try:
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    logging.warning("⚠️  pytesseract not available. Install for OCR support.")

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(str(raw).split('#', 1)[0].strip())
    except Exception:
        return default


# Sentinel stored in ocr_extracted_text when processing ran but found no text.
# Rows with this value are excluded from the backfill queue (to prevent
# infinite no-op retries when pytesseract is unavailable).
# Use --reset to NULL them out and force a fresh pass.
_OCR_NO_TEXT_SENTINEL = '__no_text__'


class SiftlyEngine:
    """Vision analysis and OCR engine for media content"""
    
    def __init__(self):
        self.db_conn = None
        self.vision_model = None
        self.processor = None
        self.embedding_model = None
        self.openrouter_client = None
        self._vision_loaded = False
        self.DEDUP_COSINE_THRESHOLD = 0.92  # Events above this similarity are duplicates
        self.recent_batch_limit = max(1, _env_int('SIFTLY_RECENT_BATCH_LIMIT', 10))
        self.backfill_batch_limit = max(1, _env_int('SIFTLY_BACKFILL_BATCH_LIMIT', 25))
        self.backfill_lookback_days = max(1, _env_int('SIFTLY_BACKFILL_LOOKBACK_DAYS', 365))
        self.backfill_enabled = os.getenv('SIFTLY_BACKFILL_ENABLED', 'true').lower() not in {'0', 'false', 'no', 'off'}
        self.hosted_vision_enabled = os.getenv('SIFTLY_HOSTED_VISION_ENABLED', 'true').lower() not in {'0', 'false', 'no', 'off'}
        
        # Load embedding model for semantic dedup + VECTOR(768) column
        # ~90MB — keep loaded since it's small and used for dedup every cycle
        if EMBEDDINGS_AVAILABLE:
            try:
                self.embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
                logger.info("✅ Loaded sentence-transformer for embeddings")
            except Exception as e:
                logger.warning(f"⚠️  Failed to load embedding model: {e}")

        if self.hosted_vision_enabled:
            try:
                self.openrouter_client = get_openrouter_client()
            except Exception as e:
                self.hosted_vision_enabled = False
                logger.warning(f"⚠️  Hosted Siftly vision unavailable: {e}")
        
    def connect_db(self):
        """Establish PostgreSQL connection with auto-reconnect"""
        try:
            self.db_conn = ensure_db_connection(self.db_conn)
            logger.info("✅ Connected to PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise
    
    def _ensure_db(self):
        """Lightweight reconnect guard"""
        self.db_conn = ensure_db_connection(self.db_conn)
    
    def load_vision_model(self):
        """Lazy-load vision-language model for image captioning (~1.5GB).
        Only called when there are actual events to process."""
        if self._vision_loaded:
            return
        if not VISION_MODEL_AVAILABLE:
            logger.warning("⚠️  Vision model not available, using fallback mode")
            return
        
        try:
            logger.info("📦 Loading BLIP vision model (lazy)...")
            self.processor = BlipProcessor.from_pretrained("Salesforce/blip-image-captioning-base")
            self.vision_model = BlipForConditionalGeneration.from_pretrained("Salesforce/blip-image-captioning-base")
            self._vision_loaded = True
            logger.info("✅ Loaded vision model")
        except Exception as e:
            logger.error(f"❌ Failed to load vision model: {e}")
            self.vision_model = None
    
    def unload_vision_model(self):
        """Free BLIP model memory when not processing."""
        if not self._vision_loaded:
            return
        self.vision_model = None
        self.processor = None
        self._vision_loaded = False
        gc.collect()
        if VISION_MODEL_AVAILABLE and torch is not None:
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
        logger.info("🗑️  Unloaded BLIP vision model to free RAM")
    
    async def download_image(self, url: str) -> Optional[Image.Image]:
        """Download image from URL"""
        try:
            response = await asyncio.to_thread(
                requests.get,
                url,
                timeout=10,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            
            if response.status_code == 200:
                return Image.open(BytesIO(response.content)).convert('RGB')
            else:
                logger.warning(f"⚠️  Failed to download image: HTTP {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Image download failed: {e}")
            return None
    
    def extract_text_ocr(self, image: Image.Image) -> str:
        """Extract text from image using OCR"""
        if not OCR_AVAILABLE:
            return ""
        
        try:
            text = pytesseract.image_to_string(image)
            return text.strip()
        except Exception as e:
            logger.error(f"❌ OCR failed: {e}")
            return ""
    
    def generate_caption(self, image: Image.Image) -> str:
        """Generate caption/description for image"""
        if not self.vision_model:
            return ""
        
        try:
            inputs = self.processor(image, return_tensors="pt")
            outputs = self.vision_model.generate(**inputs, max_length=50)
            caption = self.processor.decode(outputs[0], skip_special_tokens=True)
            return caption
        except Exception as e:
            logger.error(f"❌ Caption generation failed: {e}")
            return ""
    
    def detect_meme_format(self, image: Image.Image, ocr_text: str) -> Optional[str]:
        """Detect if image matches known meme formats"""
        # Simple heuristics - can be enhanced with ML classifier
        
        width, height = image.size
        aspect_ratio = width / height
        
        # Common meme aspect ratios
        if 0.9 < aspect_ratio < 1.1:
            # Square format (Drake, Distracted Boyfriend, etc.)
            if "nobody:" in ocr_text.lower() or "no one:" in ocr_text.lower():
                return "nobody_meme"
            return "square_meme"
        
        elif aspect_ratio > 1.5:
            # Wide format (banner memes, tweets screenshots)
            if "@" in ocr_text and ("twitter" in ocr_text.lower() or "am" in ocr_text.lower() or "pm" in ocr_text.lower()):
                return "twitter_screenshot"
            return "wide_format_meme"
        
        return None
    
    def classify_content(self, ocr_text: str, caption: str) -> List[Dict[str, float]]:
        """Classify image content into semantic tags"""
        tags = []
        text = (ocr_text + " " + caption).lower()
        
        # Esports-related
        if any(team in text for team in ['navi', 'vitality', 'faze', 'g2', 'liquid', 'astralis']):
            tags.append({'tag': 'cs2_roster', 'confidence': 0.85})
        
        # Betting/odds related
        if any(kw in text for kw in ['odds', 'line', 'spread', '+', '-', 'o/', 'u/']):
            tags.append({'tag': 'betting_odds', 'confidence': 0.90})
        
        # Financial data
        if any(kw in text for kw in ['$', '€', '£', 'revenue', 'million', 'billion']):
            tags.append({'tag': 'financial_data', 'confidence': 0.88})
        
        # Charts/graphs
        if any(kw in caption for kw in ['chart', 'graph', 'plot', 'diagram']):
            tags.append({'tag': 'data_visualization', 'confidence': 0.92})
        
        return tags

    @staticmethod
    def _extract_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
        if not raw_text:
            return None
        match = re.search(r'\{.*\}', raw_text, flags=re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except Exception:
            return None

    def analyze_image_hosted(self, image_url: str) -> Dict[str, Any]:
        """Use the shared hosted multimodal path for OCR/caption/tagging."""
        if not self.hosted_vision_enabled or not self.openrouter_client:
            return {}

        prompt = """Analyze this image for a CS2/esports ingestion pipeline.

Return JSON only with this exact schema:
{
  "ocr_text": "string",
  "caption": "short plain-English description",
  "meme_format": "twitter_screenshot|square_meme|wide_format_meme|chart|none",
  "semantic_tags": [
    {"tag": "string", "confidence": 0.0}
  ]
}

Rules:
- Extract visible text into ocr_text. If none, return "".
- caption should be one short sentence.
- semantic_tags should be 0-5 short tags relevant to the image contents.
- Good tags include cs2_roster, betting_odds, data_visualization, tournament_bracket, tweet_screenshot, player_stats, meme.
- Confidence must be between 0 and 1.
- JSON only. No markdown."""

        try:
            result = self.openrouter_client.generate_vision(
                prompt=prompt,
                image_url=image_url,
                temperature=0.1,
                max_tokens=350,
            )
            parsed = self._extract_json_object(result.get('text', '')) or {}
            if parsed:
                logger.debug("✅ Hosted Siftly vision succeeded")
            return parsed
        except Exception as e:
            logger.warning(f"⚠️  Hosted Siftly vision failed: {e}")
            return {}
    
    def generate_embedding(self, text: str) -> Optional[list]:
        """Generate a 384-dim embedding from text, zero-padded to 768 for VECTOR(768) column"""
        if not self.embedding_model or not text.strip():
            return None
        
        try:
            vec = self.embedding_model.encode(text, convert_to_numpy=True)
            # all-MiniLM-L6-v2 outputs 384-dim; pad to 768 to match schema VECTOR(768)
            padded = np.zeros(768, dtype=np.float32)
            padded[:len(vec)] = vec
            return padded.tolist()
        except Exception as e:
            logger.error(f"❌ Embedding generation failed: {e}")
            return None
    
    def check_semantic_dedup(self, embedding: list) -> bool:
        """Check if a semantically similar event already exists (pgvector cosine distance).
        
        Returns True if a duplicate is found (should skip ingestion).
        """
        if embedding is None:
            return False
        
        try:
            with self.db_conn.cursor() as cur:
                # pgvector cosine distance: 1 - cosine_similarity
                # So distance < (1 - threshold) means similarity > threshold
                distance_threshold = 1.0 - self.DEDUP_COSINE_THRESHOLD
                cur.execute("""
                    SELECT id FROM twitter_bot.siftly_events
                    WHERE embedding IS NOT NULL
                    AND created_at > NOW() - INTERVAL '48 hours'
                    ORDER BY embedding <=> %s::vector
                    LIMIT 1
                """, (str(embedding),))
                
                row = cur.fetchone()
                if row:
                    # Check actual distance
                    cur.execute("""
                        SELECT embedding <=> %s::vector AS dist
                        FROM twitter_bot.siftly_events
                        WHERE id = %s
                    """, (str(embedding), row[0]))
                    dist_row = cur.fetchone()
                    if dist_row and dist_row[0] < distance_threshold:
                        logger.info(f"🔁 Semantic duplicate detected (distance={dist_row[0]:.4f}), skipping")
                        return True
            
            return False
        except Exception as e:
            logger.warning(f"⚠️  Semantic dedup check failed: {e}")
            return False
    
    async def process_siftly_event(self, event_id: str, media_urls: List[str], skip_semantic_dedup: bool = False):
        """Process a single siftly event with media"""
        try:
            results = {
                'ocr_texts': [],
                'captions': [],
                'meme_format': None,
                'semantic_tags': []
            }
            
            for url in media_urls[:3]:  # Process up to 3 images
                image = await self.download_image(url)
                if not image:
                    continue
                
                # Extract OCR text
                ocr_text = self.extract_text_ocr(image)
                if ocr_text:
                    results['ocr_texts'].append(ocr_text)
                
                # Generate caption
                caption = self.generate_caption(image)
                if caption:
                    results['captions'].append(caption)
                
                # Detect meme format
                if not results['meme_format']:
                    results['meme_format'] = self.detect_meme_format(image, ocr_text)
                
                # Classify content
                tags = self.classify_content(ocr_text, caption)
                results['semantic_tags'].extend(tags)

                # Hosted fallback keeps OCR/captions/tags working even when local
                # OCR or BLIP captioning is unavailable.
                if self.hosted_vision_enabled and (
                    not OCR_AVAILABLE
                    or not ocr_text.strip()
                    or not caption.strip()
                    or not tags
                ):
                    remote = self.analyze_image_hosted(url)
                    remote_ocr = str(remote.get('ocr_text') or '').strip()
                    remote_caption = str(remote.get('caption') or '').strip()
                    remote_meme = str(remote.get('meme_format') or '').strip().lower()
                    remote_tags = remote.get('semantic_tags') or []

                    if remote_ocr and remote_ocr not in results['ocr_texts']:
                        results['ocr_texts'].append(remote_ocr)
                    if remote_caption and remote_caption not in results['captions']:
                        results['captions'].append(remote_caption)
                    if not results['meme_format'] and remote_meme and remote_meme != 'none':
                        results['meme_format'] = remote_meme
                    if isinstance(remote_tags, list):
                        for tag in remote_tags[:5]:
                            if not isinstance(tag, dict):
                                continue
                            name = str(tag.get('tag') or '').strip()
                            confidence = float(tag.get('confidence', 0) or 0)
                            if name:
                                results['semantic_tags'].append({
                                    'tag': name,
                                    'confidence': max(0.0, min(1.0, confidence)),
                                })
                
                # Free image memory immediately (prevents OOM on high volume)
                del image
                gc.collect()
            
            # Update database
            combined_ocr = "\n".join(results['ocr_texts'])
            combined_captions = " | ".join(results['captions'])

            # If processing ran but produced nothing (pytesseract unavailable, image too
            # blurry, etc.), write a sentinel so the backfill doesn't re-queue this row
            # endlessly.  Use --reset to NULL these out for a forced re-pass.
            ocr_to_store: str
            if not combined_ocr.strip() and not combined_captions.strip():
                ocr_to_store = _OCR_NO_TEXT_SENTINEL
            else:
                ocr_to_store = combined_ocr
            
            # Generate embedding from combined text for VECTOR(768) column
            embed_text = f"{combined_ocr} {combined_captions}".strip()
            embedding = self.generate_embedding(embed_text) if embed_text else None
            
            is_duplicate = False
            if not skip_semantic_dedup and self.check_semantic_dedup(embedding):
                is_duplicate = True
                logger.info(f"⏭️  Duplicate Siftly event {event_id} — storing OCR/tags without embedding")
            
            with self.db_conn.cursor() as cur:
                if is_duplicate:
                    cur.execute("""
                        UPDATE twitter_bot.siftly_events
                        SET ocr_extracted_text = %s,
                            semantic_tags = %s,
                            updated_at = NOW()
                        WHERE id = %s
                    """, (
                        ocr_to_store,
                        Json(results['semantic_tags']),
                        event_id
                    ))
                else:
                    cur.execute("""
                        UPDATE twitter_bot.siftly_events
                        SET ocr_extracted_text = %s,
                            semantic_tags = %s,
                            embedding = %s::vector,
                            updated_at = NOW()
                        WHERE id = %s
                    """, (
                        ocr_to_store,
                        Json(results['semantic_tags']),
                        str(embedding) if embedding else None,
                        event_id
                    ))
                self.db_conn.commit()
            
            logger.info(f"✅ Processed Siftly event {event_id}: {len(results['semantic_tags'])} tags")
            
        except Exception as e:
            logger.error(f"❌ Failed to process Siftly event {event_id}: {e}", exc_info=True)
            try:
                self.db_conn.rollback()
            except Exception:
                pass
    
    def _fetch_recent_pending_events(self, limit: int) -> List[tuple[Any, Any]]:
        with self.db_conn.cursor() as cur:
            cur.execute("""
                SELECT id, media_urls
                FROM twitter_bot.siftly_events
                WHERE has_media = TRUE
                  AND (ocr_extracted_text IS NULL OR BTRIM(ocr_extracted_text) = '')
                  AND created_at > NOW() - INTERVAL '24 hours'
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()

    def _fetch_backfill_events(self, limit: int, lookback_days: int) -> List[tuple[Any, Any]]:
        with self.db_conn.cursor() as cur:
            cur.execute("""
                SELECT id, media_urls
                FROM twitter_bot.siftly_events
                WHERE has_media = TRUE
                  AND (
                      ocr_extracted_text IS NULL
                      OR BTRIM(ocr_extracted_text) = ''
                  )
                  AND ocr_extracted_text IS DISTINCT FROM %s
                  AND created_at <= NOW() - INTERVAL '24 hours'
                  AND created_at > NOW() - (%s * INTERVAL '1 day')
                ORDER BY created_at ASC
                LIMIT %s
            """, (_OCR_NO_TEXT_SENTINEL, lookback_days, limit))
            return cur.fetchall()

    async def _process_batch(
        self,
        pending_events: List[tuple[Any, Any]],
        label: str,
        skip_semantic_dedup: bool = False,
    ) -> int:
        if not pending_events:
            return 0

        logger.info(f"🔄 Processing {len(pending_events)} {label} Siftly events...")
        for event_id, media_urls_json in pending_events:
            media_urls = media_urls_json if isinstance(media_urls_json, list) else []
            await self.process_siftly_event(str(event_id), media_urls, skip_semantic_dedup=skip_semantic_dedup)
            await asyncio.sleep(1)
        return len(pending_events)

    async def run_cycle(self):
        """Process recent Siftly events and drain a small historical OCR backlog."""
        try:
            self._ensure_db()
            recent_events = self._fetch_recent_pending_events(self.recent_batch_limit)
            backfill_events: List[tuple[Any, Any]] = []
            if self.backfill_enabled:
                backfill_events = self._fetch_backfill_events(self.backfill_batch_limit, self.backfill_lookback_days)

            if not recent_events and not backfill_events:
                logger.debug("ℹ️  No pending Siftly events")
                return

            self.load_vision_model()
            await self._process_batch(recent_events, 'recent')
            await self._process_batch(backfill_events, 'backfill', skip_semantic_dedup=True)
            self.unload_vision_model()
        except Exception as e:
            logger.error(f"❌ Cycle failed: {e}")

    async def reset_no_text_rows(self, lookback_days: Optional[int] = None) -> int:
        """NULL out sentinel and empty rows so they re-enter the backfill queue.

        Use this when pytesseract becomes available after a period where it was
        missing, so all previously-skipped rows get a proper OCR pass.
        """
        self._ensure_db()
        max_age = f"{max(1, lookback_days or self.backfill_lookback_days)} days"
        with self.db_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE twitter_bot.siftly_events
                SET ocr_extracted_text = NULL,
                    updated_at = NOW()
                WHERE has_media = TRUE
                  AND (
                      ocr_extracted_text = %s
                      OR BTRIM(COALESCE(ocr_extracted_text, '')) = ''
                  )
                  AND created_at > NOW() - (%s)::interval
                """,
                (_OCR_NO_TEXT_SENTINEL, max_age),
            )
            count = cur.rowcount
            self.db_conn.commit()
        logger.info(f"♻️  Reset {count} siftly rows to NULL — they will re-enter the backfill queue")
        return count

    async def run_backfill(self, limit: int, lookback_days: Optional[int] = None) -> int:
        """Manually process a larger historical OCR backlog and then exit."""
        self._ensure_db()
        total_processed = 0
        remaining = max(1, limit)
        max_age_days = max(1, lookback_days or self.backfill_lookback_days)

        try:
            while remaining > 0:
                batch_size = min(self.backfill_batch_limit, remaining)
                batch = self._fetch_backfill_events(batch_size, max_age_days)
                if not batch:
                    break
                self.load_vision_model()
                processed = await self._process_batch(batch, 'manual backfill', skip_semantic_dedup=True)
                total_processed += processed
                remaining -= processed
                if processed < batch_size:
                    break
        finally:
            self.unload_vision_model()

        logger.info(f"✅ Manual Siftly backfill complete: {total_processed} events processed")
        return total_processed
    
    async def run_forever(self):
        """Main loop - processes events on demand"""
        self.connect_db()
        
        # Vision model is now LAZY-LOADED — only when events need processing
        # This saves ~1.5GB RAM when idle (which is 99% of the time)
        
        logger.info("🚀 Siftly Engine started (vision model: lazy-load)")

        if not OCR_AVAILABLE and not self.hosted_vision_enabled:
            logger.warning(
                "⚠️  pytesseract not installed and hosted vision is disabled — OCR will be skipped and rows will receive "
                "the '%s' sentinel. Install with: pip install pytesseract && apt-get install -y tesseract-ocr, "
                "or enable SIFTLY_HOSTED_VISION_ENABLED=true, then run --reset to reprocess.",
                _OCR_NO_TEXT_SENTINEL,
            )
        elif not OCR_AVAILABLE and self.hosted_vision_enabled:
            logger.info("🧠 Siftly will use hosted vision fallback for OCR/caption/tagging")
        
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"❌ Run cycle failed: {e}")
            
            # Check for new events every 2 minutes (avg 1 event/day, no rush)
            await asyncio.sleep(120)
    
    def cleanup(self):
        """Cleanup resources"""
        if self.db_conn:
            self.db_conn.close()


async def main():
    parser = argparse.ArgumentParser(description='Run the Siftly OCR engine')
    parser.add_argument('--backfill', action='store_true', help='Process historical OCR backlog and exit')
    parser.add_argument('--backfill-limit', type=int, default=None, help='Max historical rows to process in this run')
    parser.add_argument('--backfill-lookback-days', type=int, default=None, help='How far back the historical scan should look')
    parser.add_argument(
        '--reset', action='store_true',
        help='NULL out no-text-sentinel and empty rows so they re-enter the backfill queue, then exit'
    )
    args = parser.parse_args()

    engine = SiftlyEngine()
    try:
        if args.reset:
            engine.connect_db()
            lookback = max(1, args.backfill_lookback_days or engine.backfill_lookback_days)
            await engine.reset_no_text_rows(lookback_days=lookback)
        elif args.backfill:
            engine.connect_db()
            limit = max(1, args.backfill_limit or engine.backfill_batch_limit)
            lookback_days = max(1, args.backfill_lookback_days or engine.backfill_lookback_days)
            await engine.run_backfill(limit=limit, lookback_days=lookback_days)
        else:
            await engine.run_forever()
    except KeyboardInterrupt:
        logger.info("⏹️  Shutting down Siftly Engine...")
    finally:
        engine.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
