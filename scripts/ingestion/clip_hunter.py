#!/usr/bin/env python3
"""
CS2 Clip & Media Hunter — Finds viral clips, highlight images, and meme material.
Scrapes Twitter, HLTV galleries, and YouTube/Twitch clip URLs.
Downloads media, generates thumbnails, creates quote-tweet-worthy media posts.

This is the ENGAGEMENT MULTIPLIER — tweets with images get 2x reach,
tweets with video get 5x reach on X algorithm.

Sources:
  1. Viral CS2 clips from Twitter (via style_bank tweets with video)
  2. HLTV match photo galleries (player celebrations, crowd shots)
  3. CS2 Reddit top clips (via JSON API)
  4. Steam workshop screenshots (community content)
  5. Pro player stream highlights (Twitch clip URLs)
"""

import asyncio
import logging
import os
import re
import json
import hashlib
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
from curl_cffi.requests import Session as CurlSession

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.db_utils import ensure_db_connection
from processing.media_manager import get_media_manager

load_dotenv('/dev/shm/.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MEDIA_DIR = Path(__file__).parent.parent.parent / 'data' / 'media_cache'
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

CLIPS_DIR = MEDIA_DIR / 'clips'
CLIPS_DIR.mkdir(exist_ok=True)

THUMBNAILS_DIR = MEDIA_DIR / 'thumbnails'
THUMBNAILS_DIR.mkdir(exist_ok=True)


class ClipHunter:
    """Finds and downloads viral CS2 media for tweet attachments"""

    # Subreddits with CS2 highlights
    CS2_SUBREDDITS = ['GlobalOffensive', 'cs2', 'CSMemes']

    def __init__(self):
        self.db_conn = None
        self._curl = CurlSession(impersonate='chrome')
        self.media_manager = get_media_manager()

    def connect_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    def _ensure_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)

    # ─── HLTV Gallery Scraping ─────────────────────────────────────

    def scrape_hltv_galleries(self) -> List[Dict]:
        """Scrape HLTV event photo galleries for high-quality CS2 images"""
        found = []
        try:
            r = self._curl.get(
                'https://www.hltv.org/galleries',
                timeout=15,
                headers={
                    'Referer': 'https://www.hltv.org/',
                    'Accept': 'text/html',
                }
            )
            if r.status_code != 200:
                logger.warning(f"⚠️  HLTV galleries: HTTP {r.status_code}")
                return []

            # Extract gallery links
            gallery_links = re.findall(r'/gallery/(\d+)/([^"]+)', r.text)

            for gid, slug in gallery_links[:5]:  # Top 5 recent galleries
                try:
                    gr = self._curl.get(
                        f'https://www.hltv.org/gallery/{gid}/{slug}',
                        timeout=15,
                        headers={'Referer': 'https://www.hltv.org/', 'Accept': 'text/html'}
                    )
                    if gr.status_code != 200:
                        continue

                    # Extract gallery images
                    images = re.findall(
                        r'(https://img-cdn\.hltv\.org/gallerypicture/[^\s"<>&]+)',
                        gr.text
                    )
                    # Extract title
                    title_match = re.search(r'<title>([^<]+)</title>', gr.text)
                    title = title_match.group(1) if title_match else slug.replace('-', ' ')

                    for img_url in images[:3]:  # Max 3 per gallery
                        img_url = img_url.replace('&amp;', '&')
                        found.append({
                            'url': img_url,
                            'source': 'hltv_gallery',
                            'title': title,
                            'gallery_id': gid,
                            'media_type': 'image',
                        })

                    await_time = 2  # Polite
                except Exception as e:
                    logger.debug(f"⚠️  Gallery {gid}: {e}")
                    continue

            logger.info(f"📸 HLTV galleries: {len(found)} images found")

        except Exception as e:
            logger.warning(f"⚠️  HLTV gallery scrape failed: {e}")

        return found

    # ─── Reddit Clip Discovery ────────────────────────────────────

    def scrape_reddit_clips(self, subreddit: str = 'GlobalOffensive', limit: int = 10) -> List[Dict]:
        """Scrape top CS2 clips from Reddit via JSON API (no auth needed)"""
        found = []
        try:
            r = self._curl.get(
                f'https://old.reddit.com/r/{subreddit}/hot.json?limit={limit}',
                timeout=15,
                headers={
                    'User-Agent': 'cs2bot:v1.0 (by /u/skinbethub)',
                    'Accept': 'application/json',
                }
            )
            if r.status_code != 200:
                logger.warning(f"⚠️  Reddit r/{subreddit}: HTTP {r.status_code}")
                return []

            data = r.json()
            posts = data.get('data', {}).get('children', [])

            for post in posts:
                pd = post.get('data', {})
                title = pd.get('title', '')
                score = pd.get('score', 0)
                url = pd.get('url', '')
                permalink = pd.get('permalink', '')
                is_video = pd.get('is_video', False)
                media = pd.get('media')

                # Only high-engagement posts
                if score < 100:
                    continue

                # Clip sources: Reddit video, Twitch clips, YouTube shorts, Medal.tv, Streamable
                clip_url = None
                media_type = 'image'

                if is_video and media:
                    reddit_video = media.get('reddit_video', {})
                    clip_url = reddit_video.get('fallback_url')
                    media_type = 'video'
                elif 'clips.twitch.tv' in url or 'twitch.tv/clip' in url:
                    clip_url = url
                    media_type = 'twitch_clip'
                elif 'youtube.com/shorts' in url or 'youtu.be' in url:
                    clip_url = url
                    media_type = 'youtube_short'
                elif 'medal.tv' in url or 'streamable.com' in url:
                    clip_url = url
                    media_type = 'video'
                elif any(url.lower().endswith(ext) for ext in ['.jpg', '.png', '.gif', '.webp']):
                    clip_url = url
                    media_type = 'image'
                elif pd.get('preview', {}).get('images'):
                    # Get preview image from any post
                    previews = pd['preview']['images']
                    if previews:
                        clip_url = previews[0].get('source', {}).get('url', '').replace('&amp;', '&')
                        media_type = 'image'

                if clip_url:
                    found.append({
                        'url': clip_url,
                        'source': f'reddit_{subreddit}',
                        'title': title[:200],
                        'score': score,
                        'permalink': f'https://reddit.com{permalink}',
                        'media_type': media_type,
                    })

            logger.info(f"📹 Reddit r/{subreddit}: {len(found)} clips found")

        except Exception as e:
            logger.warning(f"⚠️  Reddit r/{subreddit} scrape failed: {e}")

        return found

    # ─── Media Download ───────────────────────────────────────────

    def download_media(self, url: str, media_type: str = 'image') -> Optional[str]:
        """Download media to local cache, return path"""
        try:
            ext_map = {
                'image': '.jpg',
                'video': '.mp4',
                'twitch_clip': '.mp4',
                'youtube_short': '.mp4',
            }
            ext = ext_map.get(media_type, '.jpg')

            # For URLs with clear extensions, use those
            for e in ['.png', '.gif', '.webp', '.jpeg', '.mp4']:
                if e in url.lower():
                    ext = e
                    break

            fname = hashlib.md5(url.encode()).hexdigest() + ext
            local_path = MEDIA_DIR / fname

            if local_path.exists():
                return str(local_path)

            headers = {
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
            }
            if 'hltv.org' in url:
                headers['Referer'] = 'https://www.hltv.org/'
            if 'reddit' in url or 'redd.it' in url:
                headers['Referer'] = 'https://www.reddit.com/'

            r = self._curl.get(url, timeout=30, headers=headers)
            if r.status_code != 200:
                logger.warning(f"⚠️  Download failed: {url} → {r.status_code}")
                return None

            if len(r.content) < 1000:
                logger.warning(f"⚠️  File too small: {url}")
                return None

            # X has a 15MB image / 512MB video limit
            if media_type == 'image' and len(r.content) > 5 * 1024 * 1024:
                logger.warning(f"⚠️  Image too large: {len(r.content)//1024//1024}MB")
                return None

            with open(local_path, 'wb') as f:
                f.write(r.content)

            logger.info(f"📥 Downloaded: {fname} ({len(r.content)//1024}KB)")
            return str(local_path)

        except Exception as e:
            logger.warning(f"⚠️  Download error: {e}")
            return None

    def generate_thumbnail(self, video_path: str) -> Optional[str]:
        """Extract a thumbnail frame from a video using ffmpeg"""
        try:
            out_path = THUMBNAILS_DIR / (Path(video_path).stem + '_thumb.jpg')
            if out_path.exists():
                return str(out_path)

            # Extract frame at 2 seconds (usually a good moment)
            result = subprocess.run(
                ['ffmpeg', '-i', video_path, '-ss', '2', '-vframes', '1',
                 '-vf', 'scale=1280:-2', '-q:v', '2', str(out_path)],
                capture_output=True, timeout=30
            )
            if result.returncode == 0 and out_path.exists():
                logger.info(f"🖼️  Thumbnail generated: {out_path.name}")
                return str(out_path)
        except Exception as e:
            logger.debug(f"⚠️  Thumbnail failed: {e}")
        return None

    def compress_video_for_twitter(self, video_path: str, max_mb: int = 15) -> Optional[str]:
        """Compress video to fit Twitter's upload limit (15MB for images, ~512MB for video)"""
        try:
            file_size = os.path.getsize(video_path)
            if file_size <= max_mb * 1024 * 1024:
                return video_path  # Already small enough

            out_path = str(Path(video_path).with_suffix('.compressed.mp4'))
            if os.path.exists(out_path):
                return out_path

            # Target: 720p, reasonable bitrate
            result = subprocess.run(
                ['ffmpeg', '-i', video_path,
                 '-vf', 'scale=-2:720',
                 '-c:v', 'libx264', '-preset', 'fast', '-crf', '28',
                 '-c:a', 'aac', '-b:a', '128k',
                 '-movflags', '+faststart',
                 '-y', out_path],
                capture_output=True, timeout=120
            )
            if result.returncode == 0 and os.path.exists(out_path):
                new_size = os.path.getsize(out_path)
                logger.info(f"🎬 Compressed: {file_size//1024//1024}MB → {new_size//1024//1024}MB")
                return out_path
        except Exception as e:
            logger.warning(f"⚠️  Video compression failed: {e}")
        return video_path

    # ─── Storage ──────────────────────────────────────────────────

    def store_media(self, items: List[Dict]):
        """Store discovered media items in DB"""
        self._ensure_db()
        stored = 0

        for item in items:
            try:
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO twitter_bot.media_library
                        (source, source_url, title, media_type, local_path,
                         engagement_score, metadata, discovered_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (source_url) DO UPDATE SET
                            engagement_score = GREATEST(
                                twitter_bot.media_library.engagement_score,
                                EXCLUDED.engagement_score
                            ),
                            discovered_at = NOW()
                    """, (
                        item['source'],
                        item['url'],
                        item.get('title', ''),
                        item['media_type'],
                        item.get('local_path'),
                        item.get('score', item.get('engagement_score', 0)),
                        Json(item),
                    ))
                    self.db_conn.commit()
                    stored += 1
            except Exception as e:
                logger.warning(f"⚠️  Store error: {e}")
                self.db_conn.rollback()

        logger.info(f"💾 Stored {stored}/{len(items)} media items")

    def get_best_media_for_topic(self, topic: str, media_type: str = None) -> Optional[Dict]:
        """
        Find the best unused media for a given topic.
        Called by tweet_scheduler when generating media-rich tweets.
        """
        self._ensure_db()
        try:
            params = [f"%{topic}%", f"%{topic}%"]
            if media_type:
                sql = """
                    SELECT id, source_url, local_path, media_type, title, engagement_score
                    FROM twitter_bot.media_library
                    WHERE (title ILIKE %s OR source ILIKE %s)
                    AND media_type = %s
                    AND used_count < 2
                    AND discovered_at > NOW() - INTERVAL '7 days'
                    ORDER BY engagement_score DESC
                    LIMIT 1
                """
                params.append(media_type)
            else:
                sql = """
                    SELECT id, source_url, local_path, media_type, title, engagement_score
                    FROM twitter_bot.media_library
                    WHERE (title ILIKE %s OR source ILIKE %s)
                    AND used_count < 2
                    AND discovered_at > NOW() - INTERVAL '7 days'
                    ORDER BY engagement_score DESC
                    LIMIT 1
                """

            with self.db_conn.cursor() as cur:
                cur.execute(sql, params)

                row = cur.fetchone()
                if row:
                    # Increment use count
                    cur.execute("""
                        UPDATE twitter_bot.media_library
                        SET used_count = used_count + 1, last_used_at = NOW()
                        WHERE id = %s
                    """, (row[0],))
                    self.db_conn.commit()

                    return {
                        'id': str(row[0]),
                        'url': row[1],
                        'local_path': row[2],
                        'media_type': row[3],
                        'title': row[4],
                        'score': row[5],
                    }
        except Exception as e:
            logger.warning(f"⚠️  Media lookup failed: {e}")

        return None

    # ─── Main Cycle ───────────────────────────────────────────────

    async def run_cycle(self):
        """Discover and download viral CS2 media"""
        self.connect_db()
        all_media = []

        # 1. HLTV photo galleries
        try:
            hltv_media = self.scrape_hltv_galleries()
            for item in hltv_media:
                local = self.download_media(item['url'], 'image')
                if local:
                    item['local_path'] = local
                    all_media.append(item)
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"❌ HLTV galleries: {e}")

        # 2. Reddit clips
        for sub in self.CS2_SUBREDDITS:
            try:
                clips = self.scrape_reddit_clips(sub, limit=15)
                for item in clips:
                    if item['media_type'] == 'image':
                        local = self.download_media(item['url'], 'image')
                        if local:
                            item['local_path'] = local
                            all_media.append(item)
                    # Video downloads are expensive — just store the URL for now
                    elif item['media_type'] == 'video' and item.get('url'):
                        local = self.download_media(item['url'], 'video')
                        if local:
                            local = self.compress_video_for_twitter(local)
                            local = self.media_manager.prepare_video_asset(local) or local
                            thumb = self.generate_thumbnail(local)
                            item['local_path'] = local
                            if thumb:
                                item['thumbnail_path'] = thumb
                            all_media.append(item)
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"❌ Reddit r/{sub}: {e}")

        if all_media:
            self.store_media(all_media)

        images = sum(1 for m in all_media if m['media_type'] == 'image')
        videos = sum(1 for m in all_media if m['media_type'] in ('video', 'twitch_clip'))
        logger.info(f"📊 Media hunt complete: {images} images, {videos} videos")

    async def run_forever(self):
        """Main loop — every 4 hours"""
        logger.info("🚀 Clip Hunter started — finding viral CS2 media")
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"❌ Cycle failed: {e}")
            await asyncio.sleep(4 * 3600)  # 4 hours


_clip_hunter = None

def get_clip_hunter() -> ClipHunter:
    global _clip_hunter
    if _clip_hunter is None:
        _clip_hunter = ClipHunter()
    return _clip_hunter


if __name__ == '__main__':
    asyncio.run(ClipHunter().run_forever())
