#!/usr/bin/env python3
"""
CS2 Screenshot Analyzer — AI Vision for live game screenshots.

Takes a Twitch/CS2 screenshot and extracts EVERYTHING a CS2 fan would care about:
  - Teams, scores, round number, map, time remaining
  - Player alive counts, weapons, economy
  - Tournament name, stage, format
  - Tactical situation (eco vs full buy, clutch scenarios, etc.)
  - Narrative context (comeback, domination, upset, etc.)

Uses LLM Vision (Gemini Flash via OpenRouter) — far superior to OCR for CS2 HUD
because it understands the visual layout, team logos, weapon silhouettes, minimap
positions, and contextual meaning (not just raw text).

Output is structured JSON that feeds into:
  1. Real-time tweet generation ("Round 14: Zaezdzacze eco-ing at 6-7 on Mirage")
  2. Statsmeister-style stat cards (auto-generated from extracted data)
  3. MischiefCS2-style data graphics (Major race, team form, etc.)
  4. Live match narration threads
  5. Clutch/ace/highlight moment detection

This is the "eyes" of the bot — it can SEE the game.
"""

import json
import logging
import os
import re
import time
from typing import Dict, Any, Optional, List
from pathlib import Path

from dotenv import load_dotenv
load_dotenv('/dev/shm/.env')

logger = logging.getLogger(__name__)


# ─── Vision Prompts ───────────────────────────────────────────────

SCREENSHOT_ANALYSIS_PROMPT = """You are an expert CS2 analyst looking at a live tournament stream screenshot.
Extract ALL visible game information as structured JSON. Be precise — this data drives real-time tweets.

Return ONLY valid JSON with these fields (omit fields you can't determine):

{
  "tournament": "event name visible in HUD",
  "stage": "group stage / playoffs / lower bracket / etc",
  "map": "map name (Mirage, Inferno, Dust2, Nuke, Anubis, Ancient, Vertigo, Overpass, Train)",
  "team1": {"name": "team name", "score": round_wins_int, "side": "CT or T"},
  "team2": {"name": "team name", "score": round_wins_int, "side": "CT or T"},
  "round": round_number_int,
  "time_remaining": "MM:SS or null",
  "phase": "freezetime / live / bomb_planted / round_over / halftime / timeout",
  "players_alive": {"team1": count_int, "team2": count_int},
  "bomb_status": "carried / planted / defusing / null",
  "economy_read": {
    "team1": "full_buy / half_buy / eco / force_buy / pistol",
    "team2": "full_buy / half_buy / eco / force_buy / pistol"
  },
  "notable_weapons": ["AWP on player X", "player Y has AK with no armor"],
  "narrative": "one-sentence description of what's happening tactically",
  "moment_type": "normal / clutch_1vN / eco_upset / comeback / blowout / overtime / match_point / ace",
  "spectated_player": "name of player being spectated (POV shown)",
  "maps_series": [{"map": "Mirage", "score1": 16, "score2": 13}, ...],
  "veto_visible": ["maps if veto is shown"]
}

CRITICAL RULES:
- Read team names from the HUD scoreboard at the top, not guessing
- Read exact round scores and round number from the HUD
- Identify the map from the radar/minimap shape or map name text
- Read player names, health, weapons from the bottom scoreboard panels
- Detect economy from visible money ($) and weapon loadouts
- If the screenshot shows a scoreboard/stats overlay, extract those stats instead
- If it's a replay/highlight, note that in narrative"""

SCOREBOARD_ANALYSIS_PROMPT = """You are an expert CS2 analyst looking at a match scoreboard or stats overlay.
Extract ALL player statistics visible. Return ONLY valid JSON:

{
  "type": "scoreboard / end_of_map / post_match / stats_overlay",
  "map": "map name",
  "team1": {
    "name": "team name",
    "score": round_wins_int,
    "players": [
      {"name": "player", "kills": K, "deaths": D, "assists": A, "adr": ADR, "rating": R, "hs_pct": "HS%"}
    ]
  },
  "team2": {
    "name": "team name",
    "score": round_wins_int,
    "players": [
      {"name": "player", "kills": K, "deaths": D, "assists": A, "adr": ADR, "rating": R, "hs_pct": "HS%"}
    ]
  },
  "mvp": "player with best stats",
  "mvp_rating": "their rating",
  "narrative": "one-line summary of the scoreboard story"
}"""

MOMENT_NARRATOR_PROMPT = """You are a hype CS2 caster/analyst. Given this screenshot data, write a SHORT (1-2 sentence)
live narration in the style of a CS2 Twitter account. This will be tweeted in real-time.

Rules:
- Use CS2 slang naturally (eco, force buy, full save, anti-eco, clutch, trade, rotate, etc.)
- Reference specific round numbers, scores, economy
- Build narrative tension ("match point!", "on the brink of elimination", "miracle eco")
- Don't use hashtags, don't @mention anyone
- Max 240 characters for tweet compatibility
- ALL CAPS for emphasis on ONE key phrase only
- Include relevant emoji sparingly (1-2 max)

Examples of good narration:
- "ZAEZDZACZE on a pistol at 6-7 on Mirage 😬 Sparta have all the momentum with a full buy"
- "Round 14 and donk is ALONE in a 1v3 retake on A site. Spirit need this."  
- "13-5 → 13-13. The comeback is REAL. NaVi refuse to die on Inferno"
- "FaZe eco round with Deagles at 10-12... if they convert this it's game over 🔥"

Now narrate this game state:"""


class ScreenshotAnalyzer:
    """AI-powered CS2 screenshot reader using LLM Vision."""

    def __init__(self):
        from openrouter_client import get_openrouter_client
        self.client = get_openrouter_client()
        self._last_game_state: Optional[Dict] = None
        self._state_history: List[Dict] = []

    def analyze_screenshot(self, image_path: str) -> Dict[str, Any]:
        """
        Core method: Feed a CS2 screenshot to LLM Vision and get structured game data.
        Returns parsed JSON dict with teams, scores, round, economy, etc.
        """
        try:
            result = self.client.generate_vision(
                prompt=SCREENSHOT_ANALYSIS_PROMPT,
                image_path=image_path,
                tier='vision',  # Gemini Flash — fast + cheap + great vision
                temperature=0.1,
                max_tokens=800,
            )

            text = result.get('text', '')
            # Extract JSON from response (may be wrapped in ```json blocks)
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                game_state = json.loads(json_match.group())
                game_state['_analysis_model'] = result.get('model', 'unknown')
                game_state['_tokens_used'] = result.get('usage', {}).get('total_tokens', 0)

                # Track state for narrative detection
                self._state_history.append(game_state)
                if len(self._state_history) > 30:
                    self._state_history = self._state_history[-30:]
                self._last_game_state = game_state

                logger.info(f"👁️  Screenshot analyzed: {game_state.get('team1', {}).get('name', '?')} "
                          f"{game_state.get('team1', {}).get('score', '?')}-"
                          f"{game_state.get('team2', {}).get('score', '?')} "
                          f"{game_state.get('team2', {}).get('name', '?')} "
                          f"R{game_state.get('round', '?')} on {game_state.get('map', '?')}")
                return game_state
            else:
                logger.warning(f"⚠️  No JSON found in vision response: {text[:200]}")
                return {'error': 'no_json', 'raw': text}

        except json.JSONDecodeError as e:
            logger.warning(f"⚠️  JSON parse error in vision response: {e}")
            return {'error': 'json_parse', 'raw': text}
        except Exception as e:
            logger.error(f"❌ Screenshot analysis failed: {e}")
            return {'error': str(e)}

    def analyze_scoreboard(self, image_path: str) -> Dict[str, Any]:
        """
        Analyze a scoreboard/stats overlay screenshot.
        Returns structured player stats (K/D/A, ADR, rating, etc.)
        """
        try:
            result = self.client.generate_vision(
                prompt=SCOREBOARD_ANALYSIS_PROMPT,
                image_path=image_path,
                tier='vision',
                temperature=0.1,
                max_tokens=1200,
            )

            text = result.get('text', '')
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                data = json.loads(json_match.group())
                logger.info(f"📊 Scoreboard analyzed: {data.get('type', '?')}")
                return data
            return {'error': 'no_json', 'raw': text}

        except Exception as e:
            logger.error(f"❌ Scoreboard analysis failed: {e}")
            return {'error': str(e)}

    def generate_live_narration(self, game_state: Dict) -> str:
        """
        Generate a hype tweet-length narration from game state data.
        This is the "voice" — turns dry data into viral content.
        """
        try:
            state_summary = json.dumps(game_state, indent=2, default=str)

            result = self.client.generate(
                prompt=MOMENT_NARRATOR_PROMPT + f"\n\n{state_summary}",
                tier='auto',
                temperature=0.8,
                max_tokens=100,
            )

            narration = result.get('text', '').strip()
            narration = self._clean_narration(narration)

            if len(narration) > 280:
                narration = narration[:277] + '...'

            if len(narration) < 15:
                logger.warning(f"⚠️  Narration too short after cleaning: {narration}")
                return ""

            logger.info(f"🎙️  Narration: {narration[:80]}...")
            return narration

        except Exception as e:
            logger.warning(f"⚠️  Narration generation failed: {e}")
            return ""

    @staticmethod
    def _clean_narration(text: str) -> str:
        """Strip chain-of-thought reasoning, quotes, and meta-commentary from LLM output."""
        # Remove XML-style thinking tags (DeepSeek, Claude)
        text = re.sub(r'<think(?:ing)?>[^<]*</think(?:ing)?>', '', text, flags=re.DOTALL).strip()

        # Remove quotes wrapping
        text = text.strip('"\'')

        # Detect chain-of-thought reasoning patterns and extract the actual narration
        cot_patterns = [
            r'(?:okay|ok|let me|alright|sure|here)[^.]*?[.!]\s*',  # "Okay, let me think about this."
            r'(?:I need to|I should|I\'ll|Let\'s|The user wants)[^.]*?[.!]\s*',  # "I need to write..."
            r'(?:Based on|Looking at|Given|Considering)[^.]*?[.!]\s*',  # "Based on the data..."
        ]

        for pattern in cot_patterns:
            # If the text starts with reasoning, strip it
            match = re.match(pattern, text, re.IGNORECASE)
            if match and len(text) - match.end() > 20:
                text = text[match.end():].strip()

        # If text still has reasoning markers, try to find a quoted narration
        if any(m in text.lower() for m in ['i need to', 'let me', 'the user', 'i should']):
            # Look for quoted text (the actual narration)
            quoted = re.findall(r'"([^"]{20,280})"', text)
            if quoted:
                text = quoted[-1]  # Last quoted string is usually the narration

        # Final cleanup
        text = text.strip('"\'').strip()

        # Remove hashtags (narration prompt says no hashtags)
        text = re.sub(r'\s*#\S+', '', text).strip()

        return text

    def detect_highlight_moment(self, game_state: Dict) -> Optional[Dict[str, Any]]:
        """
        Detect if the current game state is a tweet-worthy highlight moment.
        Returns moment info dict or None if it's just a normal round.

        Detectable moments:
        - Clutch situation (1v2, 1v3, etc.)
        - Match point
        - Overtime
        - Eco upset attempt
        - Massive comeback (trailing by 5+ then catching up)
        - Ace potential
        - Technical timeout (drama)
        """
        moment = game_state.get('moment_type', 'normal')
        t1 = game_state.get('team1', {})
        t2 = game_state.get('team2', {})
        s1 = t1.get('score', 0) or 0
        s2 = t2.get('score', 0) or 0
        alive1 = game_state.get('players_alive', {}).get('team1', 5)
        alive2 = game_state.get('players_alive', {}).get('team2', 5)
        econ1 = game_state.get('economy_read', {}).get('team1', '')
        econ2 = game_state.get('economy_read', {}).get('team2', '')

        highlights = []

        # Clutch detection
        if alive1 == 1 and alive2 >= 2:
            highlights.append({
                'type': 'clutch',
                'detail': f"1v{alive2}",
                'team': t1.get('name', '?'),
                'urgency': 'high' if alive2 >= 3 else 'medium',
            })
        elif alive2 == 1 and alive1 >= 2:
            highlights.append({
                'type': 'clutch',
                'detail': f"1v{alive1}",
                'team': t2.get('name', '?'),
                'urgency': 'high' if alive1 >= 3 else 'medium',
            })

        # Match point
        if s1 == 12 or s2 == 12:
            leader = t1.get('name') if s1 == 12 else t2.get('name')
            highlights.append({
                'type': 'match_point',
                'detail': f"{leader} on match point",
                'urgency': 'high',
            })

        # Overtime
        if s1 >= 13 and s2 >= 13:
            highlights.append({
                'type': 'overtime',
                'detail': f"{s1}-{s2}",
                'urgency': 'high',
            })

        # Eco upset attempt
        if econ1 == 'eco' and econ2 == 'full_buy':
            highlights.append({
                'type': 'eco_upset',
                'detail': f"{t1.get('name')} eco vs {t2.get('name')} full buy",
                'urgency': 'medium',
            })
        elif econ2 == 'eco' and econ1 == 'full_buy':
            highlights.append({
                'type': 'eco_upset',
                'detail': f"{t2.get('name')} eco vs {t1.get('name')} full buy",
                'urgency': 'medium',
            })

        # Comeback detection (compare with history)
        if len(self._state_history) >= 3:
            prev_states = self._state_history[-5:]
            for prev in prev_states:
                prev_t1 = prev.get('team1', {}).get('score', 0) or 0
                prev_t2 = prev.get('team2', {}).get('score', 0) or 0
                # Team was trailing by 5+ but now within 2
                if prev_t1 - prev_t2 >= 5 and s2 - s1 >= -2 and s2 > prev_t2 + 3:
                    highlights.append({
                        'type': 'comeback',
                        'detail': f"{t2.get('name')} from {prev_t2}-{prev_t1} to {s2}-{s1}",
                        'urgency': 'high',
                    })
                    break
                elif prev_t2 - prev_t1 >= 5 and s1 - s2 >= -2 and s1 > prev_t1 + 3:
                    highlights.append({
                        'type': 'comeback',
                        'detail': f"{t1.get('name')} from {prev_t1}-{prev_t2} to {s1}-{s2}",
                        'urgency': 'high',
                    })
                    break

        if moment and moment != 'normal':
            highlights.append({'type': moment, 'detail': '', 'urgency': 'medium'})

        if highlights:
            best = max(highlights, key=lambda h: {'high': 3, 'medium': 2, 'low': 1}.get(h.get('urgency', 'low'), 0))
            logger.info(f"🔥 Highlight detected: {best['type']} — {best.get('detail', '')}")
            return best

        return None

    def extract_match_data_for_card(self, game_state: Dict) -> Optional[Dict]:
        """
        Extract data suitable for generating stat cards / graphics.
        Returns a dict that can be fed directly to meme_generator methods.
        """
        t1 = game_state.get('team1', {})
        t2 = game_state.get('team2', {})

        if not t1.get('name') or not t2.get('name'):
            return None

        data = {
            'team1': t1.get('name', ''),
            'team2': t2.get('name', ''),
            'score1': t1.get('score', 0),
            'score2': t2.get('score', 0),
            'map': game_state.get('map', ''),
            'tournament': game_state.get('tournament', ''),
            'stage': game_state.get('stage', ''),
            'round': game_state.get('round', 0),
        }

        # Map series if visible
        if game_state.get('maps_series'):
            data['map_scores'] = game_state['maps_series']

        return data

    def full_analysis(self, image_path: str) -> Dict[str, Any]:
        """
        Complete analysis pipeline: screenshot → game state → highlight detection → narration.
        Returns everything needed to decide whether and what to tweet.
        """
        result = {
            'game_state': None,
            'highlight': None,
            'narration': None,
            'card_data': None,
            'should_tweet': False,
        }

        # 1. Analyze screenshot
        game_state = self.analyze_screenshot(image_path)
        if 'error' in game_state:
            logger.warning(f"⚠️  Analysis failed: {game_state['error']}")
            return result

        result['game_state'] = game_state

        # 2. Detect highlight moment
        highlight = self.detect_highlight_moment(game_state)
        result['highlight'] = highlight

        # 3. Generate narration if interesting
        if highlight and highlight.get('urgency') in ('high', 'medium'):
            narration = self.generate_live_narration(game_state)
            result['narration'] = narration
            result['should_tweet'] = bool(narration)

        # 4. Extract card data
        result['card_data'] = self.extract_match_data_for_card(game_state)

        return result


# ─── Major Race / Rankings Data Graphics ──────────────────────────
# Inspired by @MischiefCS2's VRS prediction graphics.
# These use HLTV-scraped ranking data + Pillow to create
# professional-looking data tables.

class DataGraphicsGenerator:
    """Generate MischiefCS2-style data infographics — rankings, Major race, team form."""

    def __init__(self):
        from meme_generator import (
            CARD_WIDTH, CARD_HEIGHT, COLORS, hex_to_rgb, OUTPUT_DIR
        )
        self.CARD_WIDTH = CARD_WIDTH
        self.CARD_HEIGHT = CARD_HEIGHT
        self.COLORS = COLORS
        self.hex_to_rgb = hex_to_rgb
        self.OUTPUT_DIR = OUTPUT_DIR

        from PIL import Image, ImageDraw, ImageFont
        self.Image = Image
        self.ImageDraw = ImageDraw
        self.ImageFont = ImageFont

        # Find fonts
        bold = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
        regular = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
        self._bold_path = bold if os.path.exists(bold) else regular
        self._regular_path = regular if os.path.exists(regular) else bold
        self._font_cache = {}

    def _font(self, bold=False, size=24):
        key = f"{'b' if bold else 'r'}_{size}"
        if key not in self._font_cache:
            path = self._bold_path if bold else self._regular_path
            self._font_cache[key] = self.ImageFont.truetype(path, size)
        return self._font_cache[key]

    def _create_tall_card(self, height=None):
        """Create a taller card for data tables (keep width, variable height)."""
        h = height or 900
        img = self.Image.new('RGB', (self.CARD_WIDTH, h), self.hex_to_rgb(self.COLORS['bg_dark']))
        draw = self.ImageDraw.Draw(img)
        # Subtle gradient
        for y in range(h):
            alpha = y / h
            r = int(15 + alpha * 8)
            g = int(25 + alpha * 8)
            b = int(35 + alpha * 8)
            draw.line([(0, y), (self.CARD_WIDTH, y)], fill=(r, g, b))
        # Bottom accent
        draw.rectangle([(0, h - 4), (self.CARD_WIDTH, h)], fill=self.hex_to_rgb(self.COLORS['accent_yellow']))
        return img, draw

    def generate_ranking_table(self, teams: List[Dict], title: str = "CS2 TEAM RANKINGS",
                                subtitle: str = None) -> Optional[str]:
        """
        Generate a clean ranking table graphic.

        Args:
            teams: [{"rank": 1, "name": "Spirit", "points": 1000, "change": "+2", "form": "WWWLW"}, ...]
            title: Header text
            subtitle: Optional subheader
        """
        import hashlib
        row_h = 48
        header_h = 120
        card_h = header_h + len(teams[:15]) * row_h + 40
        img, draw = self._create_tall_card(height=card_h)

        # Header
        draw.rectangle([(0, 0), (self.CARD_WIDTH, 5)], fill=self.hex_to_rgb(self.COLORS['accent_yellow']))
        title_font = self._font(bold=True, size=36)
        draw.text((50, 20), title, fill=self.hex_to_rgb(self.COLORS['accent_yellow']), font=title_font)

        if subtitle:
            sub_font = self._font(bold=False, size=18)
            draw.text((50, 65), subtitle, fill=self.hex_to_rgb(self.COLORS['text_gray']), font=sub_font)

        # Column headers
        col_font = self._font(bold=False, size=16)
        y = header_h - 25
        draw.text((50, y), "#", fill=self.hex_to_rgb(self.COLORS['text_dim']), font=col_font)
        draw.text((90, y), "TEAM", fill=self.hex_to_rgb(self.COLORS['text_dim']), font=col_font)
        draw.text((500, y), "POINTS", fill=self.hex_to_rgb(self.COLORS['text_dim']), font=col_font)
        draw.text((650, y), "CHANGE", fill=self.hex_to_rgb(self.COLORS['text_dim']), font=col_font)
        draw.text((800, y), "FORM", fill=self.hex_to_rgb(self.COLORS['text_dim']), font=col_font)

        draw.rectangle([(40, y + 22), (self.CARD_WIDTH - 40, y + 23)],
                        fill=self.hex_to_rgb(self.COLORS['divider']))

        # Rows
        rank_font = self._font(bold=True, size=20)
        name_font = self._font(bold=True, size=22)
        val_font = self._font(bold=False, size=20)

        y = header_h
        for i, team in enumerate(teams[:15]):
            # Alternate row background
            if i % 2 == 0:
                draw.rectangle([(40, y), (self.CARD_WIDTH - 40, y + row_h)],
                                fill=(20, 30, 42))

            rank = str(team.get('rank', i + 1))
            name = team.get('name', '?')
            points = str(team.get('points', '—'))
            change = team.get('change', '')
            form = team.get('form', '')

            # Rank number
            if int(rank) <= 3:
                rank_color = self.COLORS['accent_yellow']
            else:
                rank_color = self.COLORS['text_white']
            draw.text((55, y + 12), rank, fill=self.hex_to_rgb(rank_color), font=rank_font)

            # Team name
            draw.text((90, y + 10), name.upper(), fill=self.hex_to_rgb(self.COLORS['text_white']), font=name_font)

            # Points
            draw.text((500, y + 12), points, fill=self.hex_to_rgb(self.COLORS['text_white']), font=val_font)

            # Change indicator
            if change.startswith('+'):
                change_color = self.COLORS['accent_green']
                draw.text((650, y + 12), f"▲ {change}", fill=self.hex_to_rgb(change_color), font=val_font)
            elif change.startswith('-'):
                change_color = self.COLORS['accent_red']
                draw.text((650, y + 12), f"▼ {change}", fill=self.hex_to_rgb(change_color), font=val_font)
            elif change:
                draw.text((650, y + 12), f"  {change}", fill=self.hex_to_rgb(self.COLORS['text_dim']), font=val_font)

            # Form (W/L indicators)
            form_x = 800
            form_font = self._font(bold=True, size=16)
            for ch in form[:5]:
                if ch.upper() == 'W':
                    c = self.COLORS['accent_green']
                elif ch.upper() == 'L':
                    c = self.COLORS['accent_red']
                else:
                    c = self.COLORS['text_dim']
                draw.text((form_x, y + 14), ch.upper(), fill=self.hex_to_rgb(c), font=form_font)
                form_x += 20

            y += row_h

        # Watermark
        wm_font = self._font(bold=False, size=16)
        draw.text((self.CARD_WIDTH - 140, card_h - 30), "@SkinBetHub",
                   fill=self.hex_to_rgb(self.COLORS['text_dim']), font=wm_font)

        fname = f"ranking_{hashlib.md5(title.encode()).hexdigest()[:8]}.png"
        out = self.OUTPUT_DIR / fname
        img.save(str(out), 'PNG', quality=95)
        logger.info(f"🎨 Ranking table: {out.name}")
        return str(out)

    def generate_major_race(self, region: str, teams: List[Dict],
                             slots: int = 8, title: str = None) -> Optional[str]:
        """
        Generate a Major qualification race graphic (MischiefCS2 style).

        Args:
            region: "Europe" / "Americas" / "Asia"
            teams: [{"name": "Spirit", "points": 150, "qualified": True, "bubble": False}, ...]
            slots: Number of qualifying spots
            title: Override title
        """
        import hashlib
        row_h = 44
        header_h = 110
        card_h = header_h + len(teams[:20]) * row_h + 50
        img, draw = self._create_tall_card(height=card_h)

        # Header
        draw.rectangle([(0, 0), (self.CARD_WIDTH, 5)], fill=self.hex_to_rgb(self.COLORS['accent_yellow']))
        title_text = title or f"MAJOR RACE — {region.upper()}"
        title_font = self._font(bold=True, size=34)
        draw.text((50, 20), title_text, fill=self.hex_to_rgb(self.COLORS['accent_yellow']), font=title_font)

        sub_font = self._font(bold=False, size=18)
        draw.text((50, 60), f"Top {slots} qualify • Based on VRS rankings",
                   fill=self.hex_to_rgb(self.COLORS['text_gray']), font=sub_font)

        # Column headers
        col_font = self._font(bold=False, size=14)
        y = header_h - 20
        draw.text((50, y), "#", fill=self.hex_to_rgb(self.COLORS['text_dim']), font=col_font)
        draw.text((80, y), "TEAM", fill=self.hex_to_rgb(self.COLORS['text_dim']), font=col_font)
        draw.text((450, y), "POINTS", fill=self.hex_to_rgb(self.COLORS['text_dim']), font=col_font)
        draw.text((850, y), "STATUS", fill=self.hex_to_rgb(self.COLORS['text_dim']), font=col_font)

        draw.rectangle([(40, y + 18), (self.CARD_WIDTH - 40, y + 19)],
                        fill=self.hex_to_rgb(self.COLORS['divider']))

        # Team rows
        name_font = self._font(bold=True, size=20)
        val_font = self._font(bold=False, size=18)
        rank_font = self._font(bold=True, size=18)
        status_font = self._font(bold=True, size=16)

        y = header_h
        for i, team in enumerate(teams[:20]):
            rank = i + 1

            # Qualification line
            if rank == slots:
                # Draw thick green line after the last qualifying spot
                draw.rectangle([(40, y + row_h - 2), (self.CARD_WIDTH - 40, y + row_h)],
                                fill=self.hex_to_rgb(self.COLORS['accent_green']))
            elif rank == slots + 1:
                pass  # Could add a "BUBBLE" label

            # Row background
            if rank <= slots:
                bg = (18, 35, 28)  # Subtle green tint for qualified zone
            elif rank <= slots + 2:
                bg = (35, 30, 18)  # Yellow tint for bubble
            else:
                bg = (25, 20, 20) if i % 2 == 0 else None  # Subtle red for out

            if bg:
                draw.rectangle([(40, y), (self.CARD_WIDTH - 40, y + row_h)], fill=bg)

            name = team.get('name', '?')
            points = str(team.get('points', '—'))
            qualified = team.get('qualified', False)
            bubble = team.get('bubble', False)

            # Rank
            r_color = self.COLORS['accent_yellow'] if rank <= 3 else self.COLORS['text_white']
            draw.text((55, y + 10), str(rank), fill=self.hex_to_rgb(r_color), font=rank_font)

            # Team name
            draw.text((85, y + 8), name.upper(),
                       fill=self.hex_to_rgb(self.COLORS['text_white']), font=name_font)

            # Points (bar visualization)
            max_pts = max(t.get('points', 1) for t in teams) or 1
            bar_w = int((team.get('points', 0) / max_pts) * 350)
            bar_color = self.COLORS['accent_green'] if rank <= slots else (
                self.COLORS['accent_yellow'] if rank <= slots + 2 else self.COLORS['accent_red']
            )
            draw.rectangle([(450, y + 8), (450 + bar_w, y + 30)],
                            fill=self.hex_to_rgb(bar_color))
            draw.text((455, y + 10), points,
                       fill=self.hex_to_rgb(self.COLORS['text_white']), font=val_font)

            # Status badge
            if qualified:
                draw.text((850, y + 12), "QUALIFIED",
                           fill=self.hex_to_rgb(self.COLORS['accent_green']), font=status_font)
            elif bubble:
                draw.text((850, y + 12), "BUBBLE",
                           fill=self.hex_to_rgb(self.COLORS['accent_yellow']), font=status_font)
            elif rank > slots + 2:
                draw.text((850, y + 12), "UNLIKELY",
                           fill=self.hex_to_rgb(self.COLORS['accent_red']), font=status_font)

            y += row_h

        # Watermark
        wm_font = self._font(bold=False, size=16)
        draw.text((self.CARD_WIDTH - 140, card_h - 30), "@SkinBetHub",
                   fill=self.hex_to_rgb(self.COLORS['text_dim']), font=wm_font)

        fname = f"major_{hashlib.md5(f'{region}{len(teams)}'.encode()).hexdigest()[:8]}.png"
        out = self.OUTPUT_DIR / fname
        img.save(str(out), 'PNG', quality=95)
        logger.info(f"🎨 Major race graphic: {out.name}")
        return str(out)

    def generate_team_form_card(self, team_name: str, recent_matches: List[Dict],
                                  overall_record: str = None) -> Optional[str]:
        """
        Generate a team recent form card.

        Args:
            team_name: "FaZe"
            recent_matches: [{"opponent": "NaVi", "score": "2-1", "result": "W", "event": "BLAST"}, ...]
            overall_record: "15W-3L (83%)" or None
        """
        import hashlib
        row_h = 50
        header_h = 130
        card_h = header_h + len(recent_matches[:10]) * row_h + 50
        img, draw = self._create_tall_card(height=max(card_h, self.CARD_HEIGHT))

        # Header
        draw.rectangle([(0, 0), (self.CARD_WIDTH, 5)], fill=self.hex_to_rgb(self.COLORS['accent_yellow']))
        title_font = self._font(bold=True, size=42)
        draw.text((50, 20), team_name.upper(), fill=self.hex_to_rgb(self.COLORS['text_white']), font=title_font)

        sub_font = self._font(bold=False, size=22)
        subtitle = "RECENT FORM"
        if overall_record:
            subtitle += f"  •  {overall_record}"
        draw.text((50, 72), subtitle, fill=self.hex_to_rgb(self.COLORS['text_gray']), font=sub_font)

        # Win/Loss bar
        wins = sum(1 for m in recent_matches if m.get('result', '').upper() == 'W')
        losses = len(recent_matches) - wins
        total = max(len(recent_matches), 1)
        win_w = int((wins / total) * (self.CARD_WIDTH - 100))
        draw.rectangle([(50, 105), (50 + win_w, 118)],
                        fill=self.hex_to_rgb(self.COLORS['accent_green']))
        draw.rectangle([(50 + win_w, 105), (self.CARD_WIDTH - 50, 118)],
                        fill=self.hex_to_rgb(self.COLORS['accent_red']))

        # Match rows
        y = header_h
        opp_font = self._font(bold=True, size=22)
        score_font = self._font(bold=True, size=24)
        event_font = self._font(bold=False, size=16)
        result_font = self._font(bold=True, size=22)

        for m in recent_matches[:10]:
            res = m.get('result', '?').upper()
            opp = m.get('opponent', '?')
            score = m.get('score', '?')
            event = m.get('event', '')

            # Result indicator bar
            bar_color = self.COLORS['accent_green'] if res == 'W' else self.COLORS['accent_red']
            draw.rectangle([(50, y + 5), (55, y + row_h - 5)],
                            fill=self.hex_to_rgb(bar_color))

            # Result letter
            res_color = self.COLORS['accent_green'] if res == 'W' else self.COLORS['accent_red']
            draw.text((70, y + 12), res, fill=self.hex_to_rgb(res_color), font=result_font)

            # Opponent
            draw.text((110, y + 10), f"vs {opp.upper()}",
                       fill=self.hex_to_rgb(self.COLORS['text_white']), font=opp_font)

            # Score
            draw.text((500, y + 10), score,
                       fill=self.hex_to_rgb(self.COLORS['accent_yellow']), font=score_font)

            # Event
            draw.text((600, y + 14), event,
                       fill=self.hex_to_rgb(self.COLORS['text_dim']), font=event_font)

            # Row divider
            draw.rectangle([(50, y + row_h - 1), (self.CARD_WIDTH - 50, y + row_h)],
                            fill=self.hex_to_rgb(self.COLORS['divider']))
            y += row_h

        # Watermark
        wm_font = self._font(bold=False, size=16)
        draw.text((self.CARD_WIDTH - 140, max(card_h, self.CARD_HEIGHT) - 30), "@SkinBetHub",
                   fill=self.hex_to_rgb(self.COLORS['text_dim']), font=wm_font)

        fname = f"form_{hashlib.md5(f'{team_name}{len(recent_matches)}'.encode()).hexdigest()[:8]}.png"
        out = self.OUTPUT_DIR / fname
        img.save(str(out), 'PNG', quality=95)
        logger.info(f"🎨 Team form card: {out.name}")
        return str(out)

    def generate_map_stats_card(self, team_name: str, map_stats: List[Dict],
                                  title: str = None) -> Optional[str]:
        """
        Generate a team map pool analysis card.

        Args:
            team_name: "Spirit"
            map_stats: [{"map": "Mirage", "played": 25, "win_rate": 72, "avg_rounds": 14.2}, ...]
        """
        import hashlib
        row_h = 55
        header_h = 100
        card_h = header_h + len(map_stats[:7]) * row_h + 40
        img, draw = self._create_tall_card(height=max(card_h, self.CARD_HEIGHT))

        # Header
        draw.rectangle([(0, 0), (self.CARD_WIDTH, 5)], fill=self.hex_to_rgb(self.COLORS['accent_yellow']))
        title_text = title or f"{team_name.upper()} — MAP POOL"
        title_font = self._font(bold=True, size=36)
        draw.text((50, 20), title_text, fill=self.hex_to_rgb(self.COLORS['accent_yellow']), font=title_font)

        sub_font = self._font(bold=False, size=18)
        draw.text((50, 65), "Win rate • Maps played • Avg rounds won",
                   fill=self.hex_to_rgb(self.COLORS['text_gray']), font=sub_font)

        # Map rows with win rate bars
        y = header_h
        map_font = self._font(bold=True, size=24)
        val_font = self._font(bold=False, size=20)
        pct_font = self._font(bold=True, size=28)

        for m in map_stats[:7]:
            map_name = m.get('map', '?')
            played = m.get('played', 0)
            wr = m.get('win_rate', 0)
            avg_rds = m.get('avg_rounds', 0)

            # Map name
            draw.text((55, y + 12), map_name.upper(),
                       fill=self.hex_to_rgb(self.COLORS['text_white']), font=map_font)

            # Win rate bar
            bar_max = 500
            bar_w = int((wr / 100) * bar_max)
            if wr >= 60:
                bar_color = self.COLORS['accent_green']
            elif wr >= 45:
                bar_color = self.COLORS['accent_yellow']
            else:
                bar_color = self.COLORS['accent_red']

            draw.rectangle([(250, y + 10), (250 + bar_max, y + 35)],
                            fill=(30, 40, 50))  # Background bar
            draw.rectangle([(250, y + 10), (250 + bar_w, y + 35)],
                            fill=self.hex_to_rgb(bar_color))

            # Win rate percentage
            draw.text((260, y + 10), f"{wr}%",
                       fill=self.hex_to_rgb(self.COLORS['text_white']), font=pct_font)

            # Games played and avg rounds
            draw.text((780, y + 5), f"{played} maps",
                       fill=self.hex_to_rgb(self.COLORS['text_gray']), font=val_font)
            draw.text((780, y + 28), f"Avg {avg_rds} rds",
                       fill=self.hex_to_rgb(self.COLORS['text_dim']), font=self._font(size=14))

            # Row divider
            y += row_h

        # Watermark
        wm_font = self._font(bold=False, size=16)
        draw.text((self.CARD_WIDTH - 140, max(card_h, self.CARD_HEIGHT) - 30), "@SkinBetHub",
                   fill=self.hex_to_rgb(self.COLORS['text_dim']), font=wm_font)

        fname = f"maps_{hashlib.md5(f'{team_name}{len(map_stats)}'.encode()).hexdigest()[:8]}.png"
        out = self.OUTPUT_DIR / fname
        img.save(str(out), 'PNG', quality=95)
        logger.info(f"🎨 Map stats card: {out.name}")
        return str(out)


# Singletons
_analyzer = None
_graphics = None

def get_screenshot_analyzer() -> ScreenshotAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = ScreenshotAnalyzer()
    return _analyzer

def get_data_graphics() -> DataGraphicsGenerator:
    global _graphics
    if _graphics is None:
        _graphics = DataGraphicsGenerator()
    return _graphics
