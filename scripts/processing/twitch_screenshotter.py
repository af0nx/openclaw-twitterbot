#!/usr/bin/env python3
"""
Twitch Live Screenshot Capture — Grabs screenshots of live CS2 tournament streams.

Used by the media pipeline to attach real-time game screenshots to tweets about
live/recent matches. A live screenshot of the actual match being discussed
is 10x more engaging than a stock photo from Bing Images.

Pipeline:
  1. Check known CS2 tournament streams for live status
  2. Match stream to event (ESL match → eslcs stream, BLAST → blast, etc.)
  3. Open Twitch embed via Playwright (headless Chromium)
  4. Bypass mature content gate, wait for video to load
  5. Take 1920x1080 screenshot, crop to game area
  6. Return local path for upload

Known CS2 tournament streams (Twitch channels):
  - eslcs / elozhell — ESL events
  - blast / blastpremier — BLAST events
  - pgl / pgl_csgo — PGL events
  - casthouse_cs2 / faceit — Casthouse/FACEIT events
  - paboron — Russian coverage
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

SCREENSHOT_DIR = Path(__file__).parent.parent.parent / 'data' / 'twitch_screenshots'
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# Map tournament organizer keywords → Twitch channels to try (ordered by priority)
TOURNAMENT_STREAMS: Dict[str, List[str]] = {
    # Tier 1 organizers
    'esl': ['eslcs', 'elozhell'],
    'iem': ['eslcs', 'elozhell'],
    'pro league': ['eslcs'],
    'blast': ['blast', 'blastpremier'],
    'blast premier': ['blast', 'blastpremier'],
    'blast open': ['blast', 'blastpremier'],
    'pgl': ['pgl', 'pgl_csgo'],
    'major': ['eslcs', 'pgl', 'blast'],
    # FACEIT / Casthouse
    'faceit': ['faceit', 'casthouse_cs2'],
    'casthouse': ['casthouse_cs2'],
    'cct': ['eslcs', 'casthouse_cs2'],
    'ecl': ['ecl_csgo', 'paboron'],
    # Regional
    'perfect world': ['pwrdcs'],
    'pwr': ['pwrdcs'],
    'champions league': ['casthouse_cs2', 'eslcs'],
    'esl challenger': ['eslcs', 'elozhell'],
    'draculan': ['casthouse_cs2', 'eslcs'],
    'betboom': ['ruhub_cs', 'paboron'],
    'elisa': ['elisaesports'],
    'thunderpick': ['thunderpickesports'],
    # CIS/RU
    'ruhub': ['ruhub_cs', 'paboron'],
    'fpl': ['faceit', 'fpl_csgo'],
    # BR/SA
    'furia': ['furiatv', 'gaaborotv'],
    'esea': ['eslcs'],
    # Asian
    'skyesports': ['skyesportsindia'],
    '5eplay': ['5eplay'],
}

# Fallback: try these if no tournament match
FALLBACK_STREAMS = ['eslcs', 'blast', 'pgl', 'casthouse_cs2', 'elozhell', 'faceit', 'ruhub_cs']

# Cache: avoid re-screenshotting same stream within this window
_SCREENSHOT_COOLDOWN = 120  # seconds
_last_screenshot: Dict[str, float] = {}


class TwitchScreenshotter:
    """Captures live screenshots from CS2 tournament Twitch streams."""

    def __init__(self):
        self._browser = None
        self._context = None
        self._pw = None
        self._pages: Dict[str, Any] = {}

    async def _ensure_browser(self):
        """Lazy-init Playwright browser."""
        if self._browser and self._browser.is_connected():
            return

        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage']
        )
        self._context = await self._browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        )
        # Pre-set mature content cookie to skip age gate
        await self._context.add_cookies([{
            'name': 'mature',
            'value': 'true',
            'domain': '.twitch.tv',
            'path': '/',
        }])
        logger.info("🎬 Playwright browser launched for Twitch screenshots")

    def get_channels_for_event(self, event: dict) -> List[str]:
        """Public wrapper used by the live watcher."""
        return self._find_streams_for_event(event)

    async def _get_or_create_page(self, channel: str, timeout_s: int = 20):
        await self._ensure_browser()

        page = self._pages.get(channel)
        try:
            if page and not page.is_closed():
                return page
        except Exception:
            pass

        page = await self._context.new_page()
        embed_url = f"https://player.twitch.tv/?channel={channel}&parent=localhost&muted=true"
        await page.goto(embed_url, wait_until='networkidle', timeout=timeout_s * 1000)

        try:
            btn = page.locator('button:has-text("Start Watching")')
            if await btn.count() > 0:
                await btn.click()
                await asyncio.sleep(2)
        except Exception:
            pass

        await asyncio.sleep(3)
        page_text = await page.inner_text('body')
        offline_signals = [
            'is offline', 'channel is not available',
            'sorry, the page', 'this content is not available',
        ]
        if any(sig in page_text.lower() for sig in offline_signals):
            await page.close()
            return None

        self._pages[channel] = page
        return page

    async def _capture_page(self, channel: str, suffix: str, quality: int = 85) -> Optional[str]:
        page = await self._get_or_create_page(channel)
        if not page:
            return None

        out_path = SCREENSHOT_DIR / f"{channel}_{suffix}.jpg"
        await page.screenshot(path=str(out_path), type='jpeg', quality=quality)
        file_size = out_path.stat().st_size if out_path.exists() else 0
        if file_size < 50_000:
            out_path.unlink(missing_ok=True)
            return None
        return str(out_path)

    def _find_streams_for_event(self, event: dict) -> List[str]:
        """Determine which Twitch channels to try for a given event."""
        metadata = event.get('metadata') or {}
        if not isinstance(metadata, dict):
            metadata = {}

        event_name = (metadata.get('event') or '').lower()
        headline = (event.get('headline') or '').lower()
        content = (event.get('content') or '').lower()
        search_text = f"{event_name} {headline} {content}"

        # Try matching tournament keywords
        for keyword, channels in TOURNAMENT_STREAMS.items():
            if keyword in search_text:
                logger.debug(f"🎮 Matched tournament '{keyword}' → streams: {channels}")
                return channels

        # No match — return fallbacks
        return FALLBACK_STREAMS

    async def capture_stream(self, channel: str, timeout_s: int = 20) -> Optional[str]:
        """
        Take a screenshot of a Twitch stream.
        Returns path to JPEG screenshot, or None if stream is offline/failed.
        """
        # Cooldown check
        now = time.time()
        if channel in _last_screenshot and (now - _last_screenshot[channel]) < _SCREENSHOT_COOLDOWN:
            cached_path = SCREENSHOT_DIR / f"{channel}_latest.jpg"
            if cached_path.exists():
                logger.debug(f"📸 Using cached screenshot for {channel}")
                return str(cached_path)

        try:
            path = await self._capture_page(channel, 'latest', quality=90)
            if not path:
                logger.debug(f"📺 Stream offline or unreadable: {channel}")
                return None
            _last_screenshot[channel] = now
            file_size = Path(path).stat().st_size
            logger.info(f"📸 Twitch screenshot captured: {channel} ({file_size // 1024}KB)")
            return path

        except Exception as e:
            logger.warning(f"⚠️  Twitch screenshot failed for {channel}: {e}")
            return None

    async def capture_action_burst(
        self,
        channel: str,
        detector,
        monitor_seconds: int = 8,
        monitor_fps: float = 1.0,
        burst_count: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """Monitor a stream locally and capture a burst only when action spikes."""
        page = await self._get_or_create_page(channel)
        if not page:
            return None

        interval = max(0.5, 1.0 / max(monitor_fps, 0.25))
        previous_path = await self._capture_page(channel, 'monitor_prev', quality=55)
        if not previous_path:
            return None

        checks = max(1, int(monitor_seconds / interval))
        for idx in range(checks):
            await asyncio.sleep(interval)
            current_path = await self._capture_page(channel, f'monitor_{idx}', quality=55)
            if not current_path:
                continue

            metrics = detector.analyze_transition(previous_path, current_path)
            if metrics['triggered']:
                burst_paths = [current_path]
                for burst_idx in range(1, burst_count):
                    await asyncio.sleep(1.5)
                    burst_path = await self._capture_page(channel, f'burst_{idx}_{burst_idx}', quality=90)
                    if burst_path:
                        burst_paths.append(burst_path)

                logger.info(
                    f"🎯 Local trigger fired on {channel}: score={metrics['trigger_score']:.3f}, "
                    f"killfeed={metrics['killfeed_diff']:.3f}, overlay={metrics['overlay_diff']:.3f}"
                )
                return {
                    'channel': channel,
                    'burst_paths': burst_paths,
                    'trigger_metrics': metrics,
                }

            previous_path = current_path

        return None

    async def get_highlight_burst_for_event(
        self,
        event: dict,
        detector,
        monitor_seconds: int = 8,
        monitor_fps: float = 1.0,
        burst_count: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """Try likely channels and return a burst only when local action is detected."""
        channels = self.get_channels_for_event(event)
        for channel in channels[:2]:
            result = await self.capture_action_burst(
                channel,
                detector,
                monitor_seconds=monitor_seconds,
                monitor_fps=monitor_fps,
                burst_count=burst_count,
            )
            if result:
                return result
        return None

    async def get_screenshot_for_event(self, event: dict) -> Optional[str]:
        """
        Try to capture a live Twitch screenshot relevant to an event.
        Tries multiple channels in priority order. Returns local path or None.
        """
        channels = self._find_streams_for_event(event)

        for channel in channels[:4]:  # Don't try more than 4
            path = await self.capture_stream(channel)
            if path:
                return path

        return None

    async def close(self):
        """Clean up browser resources."""
        try:
            for page in self._pages.values():
                try:
                    if page and not page.is_closed():
                        await page.close()
                except Exception:
                    pass
            self._pages.clear()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass


# Singleton
_screenshotter = None


def get_twitch_screenshotter() -> TwitchScreenshotter:
    global _screenshotter
    if _screenshotter is None:
        _screenshotter = TwitchScreenshotter()
    return _screenshotter
