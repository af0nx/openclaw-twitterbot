#!/usr/bin/env python3
"""
Meme Generator — Dynamic CS2 meme/stat card image creation.
Uses Pillow to generate Twitter-optimized images:

Output types:
  1. STAT CARDS — "Player X: 1.45 rating in 2026" with HLTV bodyshot overlay
  2. VS CARDS — "Team A vs Team B" pre-match/result cards
  3. HOT TAKE CARDS — Bold text on CS2-themed background
  4. SCOREBOARD CARDS — Match result with map scores
  5. STREAK CARDS — "Team X: 10 win streak" with fire background

Why images matter:
  - Tweets with images get 150% more retweets (Twitter internal data)
  - Stat cards get saved/bookmarked → algorithm boost
  - Original images can't be stolen without credit → brand building
  - X algorithm HEAVILY favors native images over plain text

All images are 1200x675 (Twitter card ratio) with #0F1923 dark theme.
"""

import logging
import os
from pathlib import Path
from typing import Optional, Dict, Tuple, List
import re

from PIL import Image, ImageDraw, ImageFont
from curl_cffi.requests import Session as CurlSession
import hashlib
import io

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).parent.parent.parent / 'assets'
FONTS_DIR = ASSETS_DIR / 'fonts'
OUTPUT_DIR = Path(__file__).parent.parent.parent / 'data' / 'generated_images'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Twitter card dimensions (16:9)
CARD_WIDTH = 1200
CARD_HEIGHT = 675

# CS2 Dark Theme colors
COLORS = {
    'bg_dark': '#0F1923',      # CS2 dark blue
    'bg_card': '#1A2634',      # Slightly lighter
    'accent_yellow': '#E8B849', # CS2 gold/yellow
    'accent_blue': '#4A9FD9',   # CS2 blue accent
    'accent_red': '#E74C3C',    # Alert/upset red
    'accent_green': '#2ECC71',  # Win green
    'text_white': '#FFFFFF',
    'text_gray': '#8899AA',
    'text_dim': '#556677',
    'divider': '#2A3A4A',
}

# CS2 team brand colors — accent colors for personalized cards
TEAM_COLORS: Dict[str, str] = {
    # T1 teams
    'spirit': '#6B2FBF',     # Purple
    'team spirit': '#6B2FBF',
    'navi': '#FFDE00',       # Yellow
    'natus vincere': '#FFDE00',
    'faze': '#E03C31',       # Red
    'faze clan': '#E03C31',
    'vitality': '#FFD700',   # Gold
    'g2': '#E03C31',         # Red
    'g2 esports': '#E03C31',
    'mouz': '#E42313',       # Red
    'mousesports': '#E42313',
    'liquid': '#003C71',     # Navy blue
    'team liquid': '#003C71',
    'heroic': '#FF6600',     # Orange
    'astralis': '#FF1E26',   # Red
    'fnatic': '#FF5900',     # Orange
    'furia': '#000000',      # Black (uses gold accent)
    'big': '#111111',        # Dark
    'cloud9': '#2F9FD5',     # Blue
    'complexity': '#003DA5',  # Blue
    'ence': '#FFD700',       # Gold
    'eternal fire': '#D4AF37', # Gold
    'mibr': '#E1C564',       # Gold/yellow
    'pain': '#009B3A',       # Green
    'the mongolz': '#DC241F', # Red
    'monte': '#FF8C00',      # Orange
    'apeks': '#00A859',      # Green
    'gamerlegion': '#8B5CF6', # Purple
    'saw': '#1A1A1A',        # Dark
    'betboom': '#FF4500',    # Orange-red
}

def get_team_color(team_name: str) -> str:
    """Look up a team's brand color, fallback to accent_yellow."""
    return TEAM_COLORS.get(team_name.lower().strip(), COLORS['accent_yellow'])


def ensure_readable(rgb: Tuple[int, int, int], min_lum: int = 85) -> Tuple[int, int, int]:
    """Lift dark team colours so text stays readable on #0F1923 background."""
    lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    if lum >= min_lum:
        return rgb
    factor = min_lum / max(lum, 1)
    return tuple(min(255, int(c * factor)) for c in rgb)


# ─── Player Roles ───
# Maps player nickname → role badge shown on stat cards
PLAYER_ROLES: Dict[str, str] = {
    # Spirit
    'donk': 'Rifler', 'magixx': 'Rifler', 'chopper': 'IGL', 'zont1x': 'Rifler', 'sh1ro': 'AWPer',
    # NaVi
    's1mple': 'AWPer', 'b1t': 'Rifler', 'im': 'Rifler', 'wonderful': 'Rifler', 'aleksib': 'IGL', 'jl': 'AWPer',
    # Vitality
    'zywoo': 'AWPer', 'apeks': 'Rifler', 'flamez': 'Rifler', 'ropz': 'Rifler', 'mezii': 'Support',
    # FaZe
    'frozen': 'Rifler', 'rain': 'Rifler', 'broky': 'AWPer', 'karrigan': 'IGL', 'elige': 'Rifler',
    # G2
    'niko': 'Rifler', 'hunter': 'Rifler', 'monesy': 'AWPer', 'nexa': 'IGL', 'stewie2k': 'Support',
    # MOUZ
    'torzsi': 'AWPer', 'jimpphat': 'Rifler', 'siuhy': 'IGL', 'xertion': 'Rifler', 'boros': 'Rifler',
    # Liquid
    'yekindar': 'Entry', 'naf': 'Rifler', 'skullz': 'AWPer', 'nitro': 'IGL',
    # Heroic
    'cadian': 'AWPer/IGL', 'stavn': 'Rifler', 'sjuush': 'Rifler', 'jabbi': 'Rifler', 'kyxsan': 'IGL',
    # Astralis
    'device': 'AWPer', 'gla1ve': 'IGL', 'xyp9x': 'Support', 'buzz': 'Rifler', 'staehr': 'Rifler',
    # Cloud9
    'hobbit': 'IGL', 'ax1le': 'Rifler', 'buster': 'Rifler', 'electronic': 'Rifler',
    # ENCE
    'goofy': 'Rifler', 'dycha': 'Rifler', 'hades': 'AWPer', 'snappi': 'IGL', 'gla1ve': 'IGL',
    # Misc T1
    'twistzz': 'Rifler', 'magisk': 'Rifler', 'blameF': 'IGL',
}


def get_player_role(player_name: str) -> Optional[str]:
    """Look up a player's role, or None if unknown."""
    return PLAYER_ROLES.get(player_name.lower().strip())


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    h = hex_color.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _format_market_label(market_type: str) -> str:
    """Convert API market_type to a human-readable card label."""
    _MARKET_LABELS = {
        'match_winner': 'MATCH WINNER',
        'map_winner': 'MAP WINNER',
        'map_handicap': 'MAP HANDICAP',
        'total_maps': 'TOTAL MAPS',
        'total_rounds': 'TOTAL ROUNDS',
        'round_handicap': 'ROUND HANDICAP',
        'first_map': 'FIRST MAP WINNER',
        'pistol_round': 'PISTOL ROUND',
        'over_under': 'OVER / UNDER',
    }
    if not market_type:
        return 'MATCH WINNER'
    return _MARKET_LABELS.get(market_type, market_type.upper().replace('_', ' '))


class MemeGenerator:
    """Generate CS2-themed images for tweet attachments"""

    def __init__(self):
        self._curl = CurlSession(impersonate='chrome')
        self._font_cache: Dict[str, ImageFont.FreeTypeFont] = {}
        self._setup_fonts()

    def _setup_fonts(self):
        """Load Inter font family for premium card rendering."""
        self._font_paths: Dict[str, Optional[str]] = {
            'black': None, 'extrabold': None, 'bold': None,
            'semibold': None, 'medium': None, 'regular': None,
        }
        weight_map = {
            'black': 'Inter-Black.ttf',
            'extrabold': 'Inter-ExtraBold.ttf',
            'bold': 'Inter-Bold.ttf',
            'semibold': 'Inter-SemiBold.ttf',
            'medium': 'Inter-Medium.ttf',
            'regular': 'Inter-Regular.ttf',
        }
        for weight, filename in weight_map.items():
            path = FONTS_DIR / filename
            if path.exists():
                self._font_paths[weight] = str(path)

        # Fallback to system fonts if Inter not found
        if not any(self._font_paths.values()):
            for fp in [
                '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            ]:
                if os.path.exists(fp):
                    if 'Bold' in fp and not self._font_paths['bold']:
                        self._font_paths['bold'] = fp
                    elif not self._font_paths['regular']:
                        self._font_paths['regular'] = fp

        # Fill missing weights with nearest available
        available = [w for w, p in self._font_paths.items() if p]
        if available:
            fallback = self._font_paths[available[0]]
            for w in self._font_paths:
                if not self._font_paths[w]:
                    self._font_paths[w] = fallback

        logger.info(f"🔤 Fonts: {', '.join(f'{w}={Path(p).name if p else None}' for w, p in self._font_paths.items())}")

    def _get_font(self, bold: bool = False, size: int = 36,
                  weight: str = None) -> ImageFont.FreeTypeFont:
        """Get a font by weight (black/extrabold/bold/semibold/medium/regular) and size."""
        if weight is None:
            weight = 'bold' if bold else 'regular'
        key = f"{weight}_{size}"
        if key not in self._font_cache:
            path = (self._font_paths.get(weight)
                    or self._font_paths.get('bold')
                    or self._font_paths.get('regular'))
            if path:
                self._font_cache[key] = ImageFont.truetype(path, size)
            else:
                self._font_cache[key] = ImageFont.load_default()
        return self._font_cache[key]

    def _create_base_card(self) -> Tuple[Image.Image, ImageDraw.ImageDraw]:
        """Create a base dark card with CS2 theme"""
        img = Image.new('RGB', (CARD_WIDTH, CARD_HEIGHT), hex_to_rgb(COLORS['bg_dark']))
        draw = ImageDraw.Draw(img)

        # Subtle gradient effect: darker at top, lighter at bottom
        for y in range(CARD_HEIGHT):
            alpha = y / CARD_HEIGHT
            r = int(15 + alpha * 10)
            g = int(25 + alpha * 10)
            b = int(35 + alpha * 10)
            draw.line([(0, y), (CARD_WIDTH, y)], fill=(r, g, b))

        # Bottom accent line
        draw.rectangle(
            [(0, CARD_HEIGHT - 4), (CARD_WIDTH, CARD_HEIGHT)],
            fill=hex_to_rgb(COLORS['accent_yellow'])
        )

        return img, draw

    def _add_watermark(self, draw: ImageDraw.ImageDraw):
        """Add @SkinBetHub watermark"""
        font = self._get_font(bold=False, size=18)
        text = "@SkinBetHub"
        bbox = draw.textbbox((0, 0), text, font=font)
        x = CARD_WIDTH - (bbox[2] - bbox[0]) - 20
        y = CARD_HEIGHT - (bbox[3] - bbox[1]) - 20
        draw.text((x, y), text, fill=hex_to_rgb(COLORS['text_dim']), font=font)

    def _download_player_image(self, player_name: str) -> Optional[Image.Image]:
        """Download a player bodyshot and return as PIL Image"""
        # Import from existing media_manager to reuse HLTV_PLAYER_IDS
        try:
            from processing.media_manager import HLTV_PLAYER_IDS, PLAYER_ALIASES
            canonical = PLAYER_ALIASES.get(player_name, player_name).lower()
            player_id = HLTV_PLAYER_IDS.get(canonical)
            if not player_id:
                return None

            url = f"https://www.hltv.org/player/{player_id}/{canonical}"
            r = self._curl.get(url, timeout=12, headers={
                'Referer': 'https://www.hltv.org/',
                'Accept': 'text/html',
            })
            if r.status_code != 200:
                return None

            bodyshots = re.findall(
                r'(https://img-cdn\.hltv\.org/playerbodyshot/[^\s"<>&]+(?:&amp;[^\s"<>&]+)*)',
                r.text
            )
            if not bodyshots:
                return None

            img_url = bodyshots[0].replace('&amp;', '&')
            img_r = self._curl.get(img_url, timeout=10, headers={'Referer': 'https://www.hltv.org/'})
            if img_r.status_code == 200 and len(img_r.content) > 1000:
                return Image.open(io.BytesIO(img_r.content)).convert('RGBA')
        except Exception as e:
            logger.debug(f"⚠️  Player image download: {e}")
        return None

    def _text_wrap(self, text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> List[str]:
        """Word-wrap text to fit within max_width"""
        words = text.split()
        lines = []
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] > max_width:
                if current:
                    lines.append(current)
                current = word
            else:
                current = test
        if current:
            lines.append(current)
        return lines

    # ─── Card Generators ──────────────────────────────────────────

    def generate_stat_card(self, player_name: str, stats: Dict[str, str],
                           title: str = None) -> Optional[str]:
        """
        Generate a player stat card.

        Args:
            player_name: "donk", "s1mple", etc.
            stats: {"Rating": "1.45", "K/D": "1.32", "Maps": "87"}
            title: Optional header text

        Returns: path to generated image or None
        """
        img, draw = self._create_base_card()

        # Header
        title_text = title or f"{player_name.upper()} — 2026 Stats"
        title_font = self._get_font(bold=True, size=42)
        draw.text((60, 40), title_text, fill=hex_to_rgb(COLORS['accent_yellow']), font=title_font)

        # Divider
        draw.rectangle([(60, 100), (CARD_WIDTH - 60, 102)], fill=hex_to_rgb(COLORS['divider']))

        # Stats (left column)
        stat_font = self._get_font(bold=True, size=48)
        label_font = self._get_font(bold=False, size=24)
        y_offset = 140

        for label, value in list(stats.items())[:5]:
            draw.text((80, y_offset), str(value), fill=hex_to_rgb(COLORS['text_white']), font=stat_font)
            draw.text((80, y_offset + 52), label.upper(), fill=hex_to_rgb(COLORS['text_gray']), font=label_font)
            y_offset += 100

        # Player Image (right side)
        player_img = self._download_player_image(player_name)
        if player_img:
            # Resize to fit right side
            player_img.thumbnail((400, 550), Image.Resampling.LANCZOS)
            # Position on right
            x_pos = CARD_WIDTH - player_img.width - 40
            y_pos = CARD_HEIGHT - player_img.height - 10
            img.paste(player_img, (x_pos, y_pos), player_img)

        self._add_watermark(draw)

        # Save
        fname = f"stat_{hashlib.md5(player_name.encode()).hexdigest()[:8]}_{len(stats)}.png"
        out_path = OUTPUT_DIR / fname
        img.save(str(out_path), 'PNG', quality=95)
        logger.info(f"🎨 Stat card generated: {out_path.name}")
        return str(out_path)

    # ─── Premium VS Card ─────────────────────────────────────────

    def generate_vs_card(self, team1: str, team2: str,
                         score1: str = None, score2: str = None,
                         event_name: str = None, map_name: str = None) -> Optional[str]:
        """
        Generate a premium VS / match result card with diagonal team-color split.

        RGBA compositing with team-tinted halves, scan-line texture, centered
        badge (VS or score), and team names in readable team colors.
        """
        t1_rgb = hex_to_rgb(get_team_color(team1))
        t2_rgb = hex_to_rgb(get_team_color(team2))

        # ── RGBA base ──
        base = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT),
                         hex_to_rgb(COLORS['bg_dark']) + (255,))

        # Diagonal team-color panels
        panel = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        pd.polygon(
            [(0, 0), (CARD_WIDTH // 2 + 80, 0),
             (CARD_WIDTH // 2 - 80, CARD_HEIGHT), (0, CARD_HEIGHT)],
            fill=t1_rgb + (40,))
        pd.polygon(
            [(CARD_WIDTH // 2 + 80, 0), (CARD_WIDTH, 0),
             (CARD_WIDTH, CARD_HEIGHT), (CARD_WIDTH // 2 - 80, CARD_HEIGHT)],
            fill=t2_rgb + (40,))
        base = Image.alpha_composite(base, panel)

        # Scan-line texture
        scan = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
        sd = ImageDraw.Draw(scan)
        for y in range(0, CARD_HEIGHT, 4):
            sd.line([(0, y), (CARD_WIDTH, y)], fill=(0, 0, 0, 30))
        base = Image.alpha_composite(base, scan)

        draw = ImageDraw.Draw(base)

        # Event name pill at top
        if event_name:
            ev_font = self._get_font(weight='medium', size=20)
            bbox = draw.textbbox((0, 0), event_name.upper(), font=ev_font)
            tw = bbox[2] - bbox[0]
            x = (CARD_WIDTH - tw) // 2
            draw.rounded_rectangle(
                [(x - 16, 16), (x + tw + 16, 48)],
                radius=14, fill=(15, 25, 35, 200))
            draw.text((x, 20), event_name.upper(),
                      fill=hex_to_rgb(COLORS['text_gray']), font=ev_font)

        # Team names
        team_font = self._get_font(weight='black', size=58)
        t1_readable = ensure_readable(t1_rgb)
        t2_readable = ensure_readable(t2_rgb)

        # Team 1 left
        draw.text((70, CARD_HEIGHT // 2 - 20), team1.upper(),
                  fill=t1_readable, font=team_font)

        # Team 2 right
        bbox2 = draw.textbbox((0, 0), team2.upper(), font=team_font)
        t2w = bbox2[2] - bbox2[0]
        draw.text((CARD_WIDTH - t2w - 70, CARD_HEIGHT // 2 - 20),
                  team2.upper(), fill=t2_readable, font=team_font)

        # Center badge — dark circle with VS or score
        cx, cy = CARD_WIDTH // 2, CARD_HEIGHT // 2
        badge_r = 62
        badge = Image.new('RGBA', (badge_r * 2, badge_r * 2), (0, 0, 0, 0))
        bd = ImageDraw.Draw(badge)
        bd.ellipse([(0, 0), (badge_r * 2, badge_r * 2)],
                   fill=(15, 25, 35, 230), outline=(42, 58, 74, 255), width=2)
        base.paste(badge, (cx - badge_r, cy - badge_r - 40), badge)
        draw = ImageDraw.Draw(base)

        if score1 is not None and score2 is not None:
            score_font = self._get_font(weight='black', size=48)
            score_text = f"{score1}:{score2}"
            bbox = draw.textbbox((0, 0), score_text, font=score_font)
            sw = bbox[2] - bbox[0]
            sh = bbox[3] - bbox[1]
            draw.text((cx - sw // 2, cy - 40 - sh // 2), score_text,
                      fill=hex_to_rgb(COLORS['accent_yellow']), font=score_font)
        else:
            vs_font = self._get_font(weight='black', size=42)
            bbox = draw.textbbox((0, 0), "VS", font=vs_font)
            vw = bbox[2] - bbox[0]
            vh = bbox[3] - bbox[1]
            draw.text((cx - vw // 2, cy - 40 - vh // 2), "VS",
                      fill=hex_to_rgb(COLORS['accent_red']), font=vs_font)

        # Map name at bottom center
        if map_name:
            map_font = self._get_font(weight='medium', size=22)
            bbox = draw.textbbox((0, 0), map_name, font=map_font)
            mw = bbox[2] - bbox[0]
            draw.text(((CARD_WIDTH - mw) // 2, CARD_HEIGHT - 55),
                      map_name, fill=hex_to_rgb(COLORS['text_gray']), font=map_font)

        # Bottom accent line
        draw.rectangle([(0, CARD_HEIGHT - 4), (CARD_WIDTH, CARD_HEIGHT)],
                       fill=hex_to_rgb(COLORS['accent_yellow']))

        self._add_watermark(draw)

        final = base.convert('RGB')
        fname = f"vs_{hashlib.md5(f'{team1}{team2}'.encode()).hexdigest()[:8]}.png"
        out_path = OUTPUT_DIR / fname
        final.save(str(out_path), 'PNG', quality=95)
        logger.info(f"🎨 VS card generated: {out_path.name}")
        return str(out_path)

    def generate_hot_take_card(self, text: str, accent_color: str = None) -> Optional[str]:
        """
        DISABLED — text-on-dark-background cards are no longer generated.
        Kept as stub to avoid import errors.
        """
        return None

        # Accent bar at top
        color = accent_color or COLORS['accent_yellow']
        draw.rectangle([(0, 0), (CARD_WIDTH, 6)], fill=hex_to_rgb(color))

        # Main text — large, bold, centered
        font = self._get_font(bold=True, size=52)
        lines = self._text_wrap(text, font, CARD_WIDTH - 160, draw)

        # Calculate total text height
        line_height = 65
        total_height = len(lines) * line_height
        start_y = (CARD_HEIGHT - total_height) // 2

        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            x = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
            draw.text((x, start_y + i * line_height), line,
                       fill=hex_to_rgb(COLORS['text_white']), font=font)

        # Quotation marks (decorative)
        quote_font = self._get_font(bold=True, size=120)
        draw.text((40, start_y - 80), '"', fill=hex_to_rgb(COLORS['text_dim']), font=quote_font)

        self._add_watermark(draw)

        fname = f"take_{hashlib.md5(text.encode()).hexdigest()[:8]}.png"
        out_path = OUTPUT_DIR / fname
        img.save(str(out_path), 'PNG', quality=95)
        logger.info(f"🎨 Hot take card generated: {out_path.name}")
        return str(out_path)

    def generate_scoreboard_card(self, team1: str, team2: str,
                                  maps: List[Dict],
                                  event_name: str = None) -> Optional[str]:
        """
        Generate a multi-map scoreboard card.

        Args:
            maps: [{"map": "Mirage", "score1": 16, "score2": 13}, ...]
        """
        img, draw = self._create_base_card()

        # Header
        if event_name:
            header_font = self._get_font(bold=False, size=22)
            bbox = draw.textbbox((0, 0), event_name, font=header_font)
            x = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
            draw.text((x, 20), event_name, fill=hex_to_rgb(COLORS['text_gray']), font=header_font)

        # Team names
        team_font = self._get_font(bold=True, size=40)
        draw.text((80, 60), team1.upper(), fill=hex_to_rgb(COLORS['text_white']), font=team_font)
        bbox2 = draw.textbbox((0, 0), team2.upper(), font=team_font)
        draw.text((CARD_WIDTH - (bbox2[2] - bbox2[0]) - 80, 60), team2.upper(),
                   fill=hex_to_rgb(COLORS['text_white']), font=team_font)

        # Divider
        draw.rectangle([(60, 120), (CARD_WIDTH - 60, 122)], fill=hex_to_rgb(COLORS['divider']))

        # Map rows
        map_font = self._get_font(bold=False, size=28)
        score_font = self._get_font(bold=True, size=36)
        y = 150

        for m in maps[:5]:
            map_name = m.get('map', 'Unknown')
            s1 = str(m.get('score1', '-'))
            s2 = str(m.get('score2', '-'))

            # Map name (center)
            bbox = draw.textbbox((0, 0), map_name, font=map_font)
            cx = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
            draw.text((cx, y + 5), map_name, fill=hex_to_rgb(COLORS['text_gray']), font=map_font)

            # Score 1 (left)
            s1_color = COLORS['accent_green'] if s1 > s2 else COLORS['text_white']
            draw.text((200, y), s1, fill=hex_to_rgb(s1_color), font=score_font)

            # Score 2 (right)
            s2_color = COLORS['accent_green'] if s2 > s1 else COLORS['text_white']
            bbox = draw.textbbox((0, 0), s2, font=score_font)
            draw.text((CARD_WIDTH - 200 - (bbox[2] - bbox[0]), y), s2,
                       fill=hex_to_rgb(s2_color), font=score_font)

            # Row divider
            y += 60
            draw.rectangle([(100, y), (CARD_WIDTH - 100, y + 1)], fill=hex_to_rgb(COLORS['divider']))
            y += 20

        # Series score at bottom
        total_s1 = sum(1 for m in maps if m.get('score1', 0) > m.get('score2', 0))
        total_s2 = sum(1 for m in maps if m.get('score2', 0) > m.get('score1', 0))
        series_font = self._get_font(bold=True, size=60)
        series_text = f"{total_s1} — {total_s2}"
        bbox = draw.textbbox((0, 0), series_text, font=series_font)
        cx = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((cx, CARD_HEIGHT - 120), series_text,
                   fill=hex_to_rgb(COLORS['accent_yellow']), font=series_font)

        self._add_watermark(draw)

        fname = f"score_{hashlib.md5(f'{team1}{team2}{len(maps)}'.encode()).hexdigest()[:8]}.png"
        out_path = OUTPUT_DIR / fname
        img.save(str(out_path), 'PNG', quality=95)
        logger.info(f"🎨 Scoreboard card generated: {out_path.name}")
        return str(out_path)

    def generate_streak_card(self, team: str, streak_type: str,
                              count: int, extra: str = None) -> Optional[str]:
        """
        Generate a streak/milestone card.
        e.g. "NAVI — 10 MAP WIN STREAK 🔥"
        """
        img, draw = self._create_base_card()

        # Top accent (fire/ice based on streak type)
        if streak_type == 'win':
            accent = COLORS['accent_green']
            emoji_text = "W"
        elif streak_type == 'loss':
            accent = COLORS['accent_red']
            emoji_text = "L"
        else:
            accent = COLORS['accent_yellow']
            emoji_text = "⚡"

        draw.rectangle([(0, 0), (CARD_WIDTH, 6)], fill=hex_to_rgb(accent))

        # Team name
        team_font = self._get_font(bold=True, size=56)
        bbox = draw.textbbox((0, 0), team.upper(), font=team_font)
        cx = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((cx, 100), team.upper(), fill=hex_to_rgb(COLORS['text_white']), font=team_font)

        # Big number
        num_font = self._get_font(bold=True, size=160)
        num_text = str(count)
        bbox = draw.textbbox((0, 0), num_text, font=num_font)
        cx = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((cx, 200), num_text, fill=hex_to_rgb(accent), font=num_font)

        # Streak label
        label = f"MAP {streak_type.upper()} STREAK" if streak_type in ('win', 'loss') else streak_type.upper()
        label_font = self._get_font(bold=True, size=36)
        bbox = draw.textbbox((0, 0), label, font=label_font)
        cx = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((cx, 420), label, fill=hex_to_rgb(COLORS['text_gray']), font=label_font)

        # Extra info
        if extra:
            extra_font = self._get_font(bold=False, size=24)
            bbox = draw.textbbox((0, 0), extra, font=extra_font)
            cx = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
            draw.text((cx, 480), extra, fill=hex_to_rgb(COLORS['text_dim']), font=extra_font)

        self._add_watermark(draw)

        fname = f"streak_{hashlib.md5(f'{team}{streak_type}{count}'.encode()).hexdigest()[:8]}.png"
        out_path = OUTPUT_DIR / fname
        img.save(str(out_path), 'PNG', quality=95)
        logger.info(f"🎨 Streak card: {out_path.name}")
        return str(out_path)

    # ─── Premium Player Card ─────────────────────────────────────

    def generate_player_card(self, player_name: str, stats: Dict[str, str],
                              event_name: str = None, team_name: str = None,
                              highlight_stat: str = None) -> Optional[str]:
        """
        Premium statsmeister-style player performance card.

        Team-colored left glow + edge bar, rounded team/role badge, big rating
        in dark panel, stat grid with dividers, bodyshot with team-color
        backlight (or geometric chevron fallback).
        """
        accent_rgb = hex_to_rgb(get_team_color(team_name)) if team_name else hex_to_rgb(COLORS['accent_yellow'])
        accent_text_rgb = ensure_readable(accent_rgb)

        # ── RGBA base ──
        base = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT),
                         hex_to_rgb(COLORS['bg_dark']) + (255,))

        # Team-color left glow
        glow = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for x in range(200):
            alpha = int(35 * (1 - x / 200))
            gd.line([(x, 0), (x, CARD_HEIGHT)], fill=accent_rgb + (alpha,))
        base = Image.alpha_composite(base, glow)

        # Left edge bar
        edge = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
        ed = ImageDraw.Draw(edge)
        ed.rectangle([(0, 0), (5, CARD_HEIGHT)], fill=accent_rgb + (220,))
        base = Image.alpha_composite(base, edge)

        # Scan-line texture
        scan = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
        sd = ImageDraw.Draw(scan)
        for y in range(0, CARD_HEIGHT, 4):
            sd.line([(0, y), (CARD_WIDTH, y)], fill=(0, 0, 0, 18))
        base = Image.alpha_composite(base, scan)

        draw = ImageDraw.Draw(base)

        # Event name (top left, subtle)
        top_y = 28
        if event_name:
            ev_font = self._get_font(weight='medium', size=18)
            draw.text((55, top_y), event_name.upper(),
                      fill=hex_to_rgb(COLORS['text_dim']), font=ev_font)
            top_y += 28

        # Player name (large)
        name_font = self._get_font(weight='black', size=52)
        draw.text((55, top_y), player_name.upper(),
                  fill=hex_to_rgb(COLORS['text_white']), font=name_font)
        top_y += 62

        # Team / role badge (rounded pill)
        if team_name:
            role = get_player_role(player_name)
            badge_text = team_name.upper()
            if role:
                badge_text += f"  ·  {role.upper()}"
            badge_font = self._get_font(weight='semibold', size=18)
            bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
            bw = bbox[2] - bbox[0]
            draw.rounded_rectangle(
                [(53, top_y), (53 + bw + 24, top_y + 30)],
                radius=15, fill=accent_rgb + (50,), outline=accent_rgb + (120,))
            draw.text((65, top_y + 5), badge_text,
                      fill=accent_text_rgb, font=badge_font)
            top_y += 45

        # Highlight stat (big number in dark panel)
        stat_items = list(stats.items())
        if highlight_stat and highlight_stat in stats:
            big_value = stats[highlight_stat]
            big_label = highlight_stat
        elif stat_items:
            big_label, big_value = stat_items[0]
        else:
            big_value, big_label = None, None

        if big_value:
            panel_h = 105
            draw.rounded_rectangle(
                [(50, top_y + 5), (280, top_y + 5 + panel_h)],
                radius=12, fill=(15, 25, 35, 200))
            big_font = self._get_font(weight='black', size=72)
            draw.text((65, top_y + 10), str(big_value),
                      fill=accent_text_rgb, font=big_font)
            lbl_font = self._get_font(weight='semibold', size=18)
            draw.text((65, top_y + 82), big_label.upper(),
                      fill=hex_to_rgb(COLORS['text_gray']), font=lbl_font)
            top_y += panel_h + 20

        # Stat grid — remaining stats in a row with dividers
        remaining = stat_items[1:5] if big_value else stat_items[:4]
        if remaining:
            grid_x = 55
            col_w = 120
            val_font = self._get_font(weight='bold', size=30)
            lbl_font = self._get_font(weight='medium', size=14)
            for i, (label, value) in enumerate(remaining):
                x = grid_x + i * col_w
                if i > 0:
                    draw.line([(x - 10, top_y), (x - 10, top_y + 48)],
                              fill=hex_to_rgb(COLORS['divider']), width=1)
                draw.text((x, top_y), str(value),
                          fill=hex_to_rgb(COLORS['text_white']), font=val_font)
                draw.text((x, top_y + 34), label.upper(),
                          fill=hex_to_rgb(COLORS['text_dim']), font=lbl_font)

        # Player bodyshot (right side)
        player_img = self._download_player_image(player_name)
        if player_img:
            player_img.thumbnail((480, 600), Image.Resampling.LANCZOS)
            x_pos = CARD_WIDTH - player_img.width - 30
            y_pos = CARD_HEIGHT - player_img.height - 8

            # Team-color backlight behind player
            bl = Image.new('RGBA', (player_img.width + 40, player_img.height + 40), (0, 0, 0, 0))
            bld = ImageDraw.Draw(bl)
            bld.ellipse([(0, 40), (bl.width, bl.height)],
                        fill=accent_rgb + (25,))
            base.paste(bl, (x_pos - 20, y_pos - 20), bl)
            base.paste(player_img, (x_pos, y_pos), player_img)
        else:
            # Geometric chevron fallback
            chev = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
            cd = ImageDraw.Draw(chev)
            for i in range(5):
                y_off = 140 + i * 110
                cd.polygon(
                    [(CARD_WIDTH - 350, y_off), (CARD_WIDTH - 50, y_off + 55),
                     (CARD_WIDTH - 350, y_off + 110)],
                    fill=accent_rgb + (12 + i * 4,))
            base = Image.alpha_composite(base, chev)

        draw = ImageDraw.Draw(base)

        # Bottom accent line
        draw.rectangle([(0, CARD_HEIGHT - 4), (CARD_WIDTH, CARD_HEIGHT)],
                       fill=hex_to_rgb(COLORS['accent_yellow']))

        self._add_watermark(draw)

        final = base.convert('RGB')
        fname = f"pcard_{hashlib.md5(f'{player_name}{str(stats)}'.encode()).hexdigest()[:8]}.png"
        out_path = OUTPUT_DIR / fname
        final.save(str(out_path), 'PNG', quality=95)
        logger.info(f"🎨 Player card (premium): {out_path.name} — {player_name}")
        return str(out_path)

    # ─── Premium Match Result Card ────────────────────────────────

    def generate_match_result_card(self, team1: str, team2: str,
                                    score1: int, score2: int,
                                    map_scores: List[Dict] = None,
                                    event_name: str = None,
                                    mvp_name: str = None,
                                    mvp_rating: str = None) -> Optional[str]:
        """
        Premium match result card with winner glow, gold score with shadow,
        map rows with alternating tints + team-colored win bars, and a
        rounded MVP panel with star icon.
        """
        t1_rgb = hex_to_rgb(get_team_color(team1))
        t2_rgb = hex_to_rgb(get_team_color(team2))
        winner_name = team1 if score1 > score2 else team2
        winner_rgb = hex_to_rgb(get_team_color(winner_name))

        # ── RGBA base ──
        base = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT),
                         hex_to_rgb(COLORS['bg_dark']) + (255,))

        # Winner-side glow
        glow = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        glow_x = 0 if score1 > score2 else CARD_WIDTH - 300
        for x_off in range(300):
            alpha = int(30 * (1 - x_off / 300))
            gd.line([(glow_x + x_off, 0), (glow_x + x_off, CARD_HEIGHT)],
                    fill=winner_rgb + (alpha,))
        base = Image.alpha_composite(base, glow)

        # Scan-line texture
        scan = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
        sd = ImageDraw.Draw(scan)
        for y in range(0, CARD_HEIGHT, 4):
            sd.line([(0, y), (CARD_WIDTH, y)], fill=(0, 0, 0, 18))
        base = Image.alpha_composite(base, scan)

        draw = ImageDraw.Draw(base)

        # Event name pill at top
        if event_name:
            ev_font = self._get_font(weight='medium', size=20)
            bbox = draw.textbbox((0, 0), event_name.upper(), font=ev_font)
            ew = bbox[2] - bbox[0]
            x = (CARD_WIDTH - ew) // 2
            draw.rounded_rectangle(
                [(x - 14, 14), (x + ew + 14, 44)],
                radius=12, fill=(15, 25, 35, 200))
            draw.text((x, 18), event_name.upper(),
                      fill=hex_to_rgb(COLORS['text_gray']), font=ev_font)

        # ── Score row: Team1  SCORE  Team2 ──
        row_y = 70
        team_font = self._get_font(weight='extrabold', size=44)
        score_font = self._get_font(weight='black', size=72)

        t1_readable = ensure_readable(t1_rgb) if score1 > score2 else hex_to_rgb(COLORS['text_white'])
        t2_readable = ensure_readable(t2_rgb) if score2 > score1 else hex_to_rgb(COLORS['text_white'])

        draw.text((70, row_y + 12), team1.upper(),
                  fill=t1_readable, font=team_font)
        bbox2 = draw.textbbox((0, 0), team2.upper(), font=team_font)
        t2w = bbox2[2] - bbox2[0]
        draw.text((CARD_WIDTH - t2w - 70, row_y + 12), team2.upper(),
                  fill=t2_readable, font=team_font)

        # Score centered with shadow
        score_text = f"{score1} — {score2}"
        bbox = draw.textbbox((0, 0), score_text, font=score_font)
        sw = bbox[2] - bbox[0]
        sx = (CARD_WIDTH - sw) // 2
        draw.text((sx + 2, row_y + 2), score_text,
                  fill=(0, 0, 0, 120), font=score_font)
        draw.text((sx, row_y), score_text,
                  fill=hex_to_rgb(COLORS['accent_yellow']), font=score_font)

        # ── Map rows ──
        if map_scores:
            map_y = 175
            map_font = self._get_font(weight='medium', size=22)
            ms_font = self._get_font(weight='bold', size=26)

            for idx, m in enumerate(map_scores[:5]):
                map_name = m.get('map', '?')
                s1 = m.get('s1', m.get('score1', '-'))
                s2 = m.get('s2', m.get('score2', '-'))

                # Alternating row tint
                if idx % 2 == 0:
                    row_bg = Image.new('RGBA', (CARD_WIDTH - 120, 44),
                                       (255, 255, 255, 8))
                    base.paste(row_bg, (60, map_y - 4), row_bg)
                    draw = ImageDraw.Draw(base)

                # Team-colored win bar on winner's side
                try:
                    ns1, ns2 = int(s1), int(s2)
                    if ns1 > ns2:
                        draw.rectangle(
                            [(60, map_y - 4), (64, map_y + 38)],
                            fill=ensure_readable(t1_rgb))
                    elif ns2 > ns1:
                        draw.rectangle(
                            [(CARD_WIDTH - 64, map_y - 4),
                             (CARD_WIDTH - 60, map_y + 38)],
                            fill=ensure_readable(t2_rgb))
                except (ValueError, TypeError):
                    pass

                # Map name centered
                bbox = draw.textbbox((0, 0), map_name, font=map_font)
                mw = bbox[2] - bbox[0]
                draw.text(((CARD_WIDTH - mw) // 2, map_y + 5), map_name,
                          fill=hex_to_rgb(COLORS['text_gray']), font=map_font)

                # Scores
                s1_str, s2_str = str(s1), str(s2)
                try:
                    s1_color = hex_to_rgb(COLORS['accent_green']) if int(s1_str) > int(s2_str) else hex_to_rgb(COLORS['text_white'])
                    s2_color = hex_to_rgb(COLORS['accent_green']) if int(s2_str) > int(s1_str) else hex_to_rgb(COLORS['text_white'])
                except (ValueError, TypeError):
                    s1_color = s2_color = hex_to_rgb(COLORS['text_white'])

                draw.text((240, map_y + 2), s1_str, fill=s1_color, font=ms_font)
                bbox_s2 = draw.textbbox((0, 0), s2_str, font=ms_font)
                draw.text((CARD_WIDTH - 240 - (bbox_s2[2] - bbox_s2[0]), map_y + 2),
                          s2_str, fill=s2_color, font=ms_font)

                map_y += 48

        # ── MVP section ──
        if mvp_name:
            mvp_y = CARD_HEIGHT - 165

            # Rounded dark panel
            draw.rounded_rectangle(
                [(80, mvp_y), (CARD_WIDTH - 80, CARD_HEIGHT - 30)],
                radius=16, fill=(15, 25, 35, 220),
                outline=hex_to_rgb(COLORS['divider']))

            # Star + MVP label
            star_font = self._get_font(weight='bold', size=22)
            draw.text((105, mvp_y + 14), "★  MVP",
                      fill=hex_to_rgb(COLORS['accent_yellow']), font=star_font)

            # Player name
            mvp_name_font = self._get_font(weight='extrabold', size=34)
            draw.text((105, mvp_y + 44), mvp_name.upper(),
                      fill=hex_to_rgb(COLORS['text_white']), font=mvp_name_font)

            # Rating on right side of panel
            if mvp_rating:
                rat_font = self._get_font(weight='black', size=52)
                rat_label_font = self._get_font(weight='medium', size=14)
                bbox_r = draw.textbbox((0, 0), str(mvp_rating), font=rat_font)
                rw = bbox_r[2] - bbox_r[0]
                rx = CARD_WIDTH - 120 - rw
                draw.text((rx, mvp_y + 20), str(mvp_rating),
                          fill=hex_to_rgb(COLORS['accent_yellow']), font=rat_font)
                draw.text((rx, mvp_y + 78), "RATING",
                          fill=hex_to_rgb(COLORS['text_dim']), font=rat_label_font)

            # MVP bodyshot
            mvp_img = self._download_player_image(mvp_name)
            if mvp_img:
                mvp_img.thumbnail((130, 130), Image.Resampling.LANCZOS)
                mvp_x = CARD_WIDTH - 280
                base.paste(mvp_img, (mvp_x, mvp_y - 5), mvp_img)
                draw = ImageDraw.Draw(base)

        # Bottom accent line
        draw.rectangle([(0, CARD_HEIGHT - 4), (CARD_WIDTH, CARD_HEIGHT)],
                       fill=hex_to_rgb(COLORS['accent_yellow']))

        self._add_watermark(draw)

        final = base.convert('RGB')
        fname = f"result_{hashlib.md5(f'{team1}{team2}{score1}{score2}'.encode()).hexdigest()[:8]}.png"
        out_path = OUTPUT_DIR / fname
        final.save(str(out_path), 'PNG', quality=95)
        logger.info(f"🎨 Match result card (premium): {out_path.name}")
        return str(out_path)

    def generate_comparison_card(self, player1: str, player2: str,
                                  stats1: Dict[str, str], stats2: Dict[str, str],
                                  title: str = None) -> Optional[str]:
        """
        Generate a head-to-head player comparison card.
        @statsmeister1 style: two bodyshots facing each other, stats compared side by side.
        """
        img, draw = self._create_base_card()

        # Top accent
        draw.rectangle([(0, 0), (CARD_WIDTH, 5)], fill=hex_to_rgb(COLORS['accent_yellow']))

        # Title
        title_text = title or f"{player1.upper()} vs {player2.upper()}"
        title_font = self._get_font(bold=True, size=38)
        bbox = draw.textbbox((0, 0), title_text, font=title_font)
        tx = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((tx, 25), title_text, fill=hex_to_rgb(COLORS['accent_yellow']), font=title_font)

        # Player 1 bodyshot (left)
        p1_img = self._download_player_image(player1)
        if p1_img:
            p1_img.thumbnail((300, 450), Image.Resampling.LANCZOS)
            y_pos = CARD_HEIGHT - p1_img.height - 10
            img.paste(p1_img, (20, y_pos), p1_img)

        # Player 2 bodyshot (right)
        p2_img = self._download_player_image(player2)
        if p2_img:
            p2_img.thumbnail((300, 450), Image.Resampling.LANCZOS)
            y_pos = CARD_HEIGHT - p2_img.height - 10
            img.paste(p2_img, (CARD_WIDTH - p2_img.width - 20, y_pos), p2_img)

        # Stats comparison (center column)
        stat_font = self._get_font(bold=True, size=32)
        label_font = self._get_font(bold=False, size=18)
        val_font = self._get_font(bold=True, size=30)

        y = 90
        # Merge stat keys
        all_keys = list(dict.fromkeys(list(stats1.keys()) + list(stats2.keys())))

        for key in all_keys[:6]:
            v1 = stats1.get(key, '—')
            v2 = stats2.get(key, '—')

            # Label (center)
            bbox = draw.textbbox((0, 0), key.upper(), font=label_font)
            cx = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
            draw.text((cx, y), key.upper(), fill=hex_to_rgb(COLORS['text_dim']), font=label_font)

            # Value 1 (left of center)
            # Highlight the higher value
            try:
                n1, n2 = float(v1.replace('+', '')), float(v2.replace('+', ''))
                c1 = COLORS['accent_green'] if n1 >= n2 else COLORS['text_white']
                c2 = COLORS['accent_green'] if n2 >= n1 else COLORS['text_white']
            except (ValueError, AttributeError):
                c1 = c2 = COLORS['text_white']

            bbox1 = draw.textbbox((0, 0), str(v1), font=val_font)
            draw.text((CARD_WIDTH // 2 - (bbox1[2] - bbox1[0]) - 60, y + 22),
                       str(v1), fill=hex_to_rgb(c1), font=val_font)

            draw.text((CARD_WIDTH // 2 + 60, y + 22),
                       str(v2), fill=hex_to_rgb(c2), font=val_font)

            # Divider
            y += 80
            if y < CARD_HEIGHT - 100:
                draw.rectangle([(350, y - 10), (CARD_WIDTH - 350, y - 9)], fill=hex_to_rgb(COLORS['divider']))

        # Player names at bottom
        names_font = self._get_font(bold=True, size=28)
        draw.text((50, CARD_HEIGHT - 50), player1.upper(), fill=hex_to_rgb(COLORS['text_white']), font=names_font)
        bbox2_name = draw.textbbox((0, 0), player2.upper(), font=names_font)
        draw.text((CARD_WIDTH - (bbox2_name[2] - bbox2_name[0]) - 50, CARD_HEIGHT - 50),
                   player2.upper(), fill=hex_to_rgb(COLORS['text_white']), font=names_font)

        self._add_watermark(draw)

        fname = f"cmp_{hashlib.md5(f'{player1}{player2}'.encode()).hexdigest()[:8]}.png"
        out_path = OUTPUT_DIR / fname
        img.save(str(out_path), 'PNG', quality=95)
        logger.info(f"🎨 Comparison card: {out_path.name}")
        return str(out_path)

    def generate_prediction_card(
        self,
        pick_team: str,
        opponent: str,
        win_probability: float = 0.0,
        edge_pct: float = 0.0,
        confidence: str = 'standard',
        pick_odds: float = None,
        event_name: str = None,
        key_factor: str = None,
        market_type: str = 'match_winner',
        analysis_lines: list = None,
    ) -> Optional[str]:
        """
        Generate a prediction pick card.

        Vertical layout:
          Row 1: "OUR PICK" pill + event name
          Row 2: Market type banner (prominent when NOT match_winner)
          Row 3: Pick team (large, highlighted, ▶ arrow) — auto-scaled font
          Row 4: "vs" divider
          Row 5: Opponent team (dimmer) — auto-scaled font
          Row 6: Win probability badge + odds + edge in a horizontal strip
          Row 7: Analysis lines (up to 3, properly spaced)
          Bottom: Accent bar
        """
        try:
            pick_rgb = hex_to_rgb(get_team_color(pick_team))
            opp_rgb = hex_to_rgb(get_team_color(opponent))

            # ── RGBA base ──
            base = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT),
                             hex_to_rgb(COLORS['bg_dark']) + (255,))

            # Subtle gradient panel — pick team color wash on left
            panel = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
            pd = ImageDraw.Draw(panel)
            pd.rectangle(
                [(0, 0), (CARD_WIDTH, CARD_HEIGHT)],
                fill=pick_rgb + (20,))
            base = Image.alpha_composite(base, panel)

            # Scan-line texture
            scan = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
            sd = ImageDraw.Draw(scan)
            for y in range(0, CARD_HEIGHT, 4):
                sd.line([(0, y), (CARD_WIDTH, y)], fill=(0, 0, 0, 30))
            base = Image.alpha_composite(base, scan)

            draw = ImageDraw.Draw(base)

            conf_color = hex_to_rgb(COLORS['accent_green']) if confidence == 'strong' else hex_to_rgb(COLORS['accent_yellow'])

            # ── Row 1: "OUR PICK" pill + event name ──
            label = "OUR PICK"
            label_font = self._get_font(weight='extrabold', size=18)
            bbox = draw.textbbox((0, 0), label, font=label_font)
            tw = bbox[2] - bbox[0]
            lx = (CARD_WIDTH - tw) // 2
            draw.rounded_rectangle(
                [(lx - 20, 14), (lx + tw + 20, 48)],
                radius=14, fill=conf_color + (220,))
            draw.text((lx, 18), label,
                      fill=(15, 25, 35), font=label_font)

            # Event name
            if event_name:
                ev_font = self._get_font(weight='medium', size=16)
                ev_text = event_name.upper()
                if len(ev_text) > 50:
                    ev_text = ev_text[:47] + "..."
                bbox_ev = draw.textbbox((0, 0), ev_text, font=ev_font)
                evw = bbox_ev[2] - bbox_ev[0]
                draw.text(((CARD_WIDTH - evw) // 2, 56),
                          ev_text,
                          fill=hex_to_rgb(COLORS['text_gray']), font=ev_font)

            # ── Row 2: Market type banner ──
            # Only show prominently if it's NOT the default match_winner
            market_label = _format_market_label(market_type)
            is_special_market = market_type and market_type != 'match_winner'
            if is_special_market:
                mkt_font = self._get_font(weight='bold', size=20)
                bbox_mkt = draw.textbbox((0, 0), market_label, font=mkt_font)
                mktw = bbox_mkt[2] - bbox_mkt[0]
                mx = (CARD_WIDTH - mktw) // 2
                draw.rounded_rectangle(
                    [(mx - 16, 80), (mx + mktw + 16, 110)],
                    radius=10, fill=hex_to_rgb(COLORS['accent_blue']) + (180,))
                draw.text((mx, 84), market_label,
                          fill=hex_to_rgb(COLORS['text_white']), font=mkt_font)
                teams_y_start = 124
            else:
                teams_y_start = 88

            # ── Row 3 + 4 + 5: Pick team → VS → Opponent ──
            # Auto-scale: large names get smaller fonts
            def _fit_team_font(name: str, max_w: int, start_size: int = 52) -> tuple:
                """Return (font, text_width) that fits within max_w."""
                for sz in range(start_size, 22, -2):
                    f = self._get_font(weight='black', size=sz)
                    bb = draw.textbbox((0, 0), name.upper(), font=f)
                    w = bb[2] - bb[0]
                    if w <= max_w:
                        return f, w
                f = self._get_font(weight='black', size=22)
                bb = draw.textbbox((0, 0), name.upper(), font=f)
                return f, bb[2] - bb[0]

            usable_w = CARD_WIDTH - 160  # 80px margin each side

            # Calculate total content height to center vertically
            # We'll measure everything then offset from center
            pick_font_test, _ = _fit_team_font(pick_team, usable_w)
            opp_font_test, _ = _fit_team_font(opponent, usable_w, start_size=44)
            pick_bb_test = draw.textbbox((0, 0), pick_team.upper(), font=pick_font_test)
            pick_h_est = pick_bb_test[3] - pick_bb_test[1]
            opp_bb_test = draw.textbbox((0, 0), opponent.upper(), font=opp_font_test)
            opp_h_est = opp_bb_test[3] - opp_bb_test[1]
            # Content block: pick(h) + gap(14) + vs(30) + gap(10) + opp(h) + gap(20) + strip(50) + gap(14+20*lines)
            n_lines = min(len(analysis_lines), 3) if analysis_lines else (1 if key_factor else 0)
            content_h = pick_h_est + 14 + 30 + 10 + opp_h_est + 20 + 50 + 18 + n_lines * 20
            # Center block between teams_y_start and (CARD_HEIGHT - 40)
            available_h = (CARD_HEIGHT - 40) - teams_y_start
            y_offset = teams_y_start + max(0, (available_h - content_h) // 2)

            # Pick team — white text, always readable on dark bg
            pick_font, pick_tw = _fit_team_font(pick_team, usable_w)
            pick_x = (CARD_WIDTH - pick_tw) // 2
            pick_bb = draw.textbbox((pick_x, y_offset), pick_team.upper(), font=pick_font)
            pick_h = pick_bb[3] - pick_bb[1]
            # Arrow indicator left of team name
            arrow_font = self._get_font(weight='bold', size=28)
            draw.text((pick_x - 40, y_offset + 4), "▶",
                      fill=conf_color, font=arrow_font)
            # Team name in white with colored underline
            draw.text((pick_x, y_offset), pick_team.upper(),
                      fill=hex_to_rgb(COLORS['text_white']), font=pick_font)
            # Colored accent underline — below actual rendered bottom
            underline_y = pick_bb[3] + 6
            draw.line(
                [(pick_x, underline_y), (pick_x + pick_tw, underline_y)],
                fill=pick_rgb + (200,), width=3)

            # "VS" divider
            vs_y = underline_y + 8
            vs_font = self._get_font(weight='semibold', size=20)
            bbox_vs = draw.textbbox((0, 0), "VS", font=vs_font)
            vsw = bbox_vs[2] - bbox_vs[0]
            line_y = vs_y + 10
            draw.line([(80, line_y), (CARD_WIDTH // 2 - vsw // 2 - 16, line_y)],
                      fill=hex_to_rgb(COLORS['divider']), width=1)
            draw.text(((CARD_WIDTH - vsw) // 2, vs_y),
                      "VS", fill=hex_to_rgb(COLORS['text_dim']), font=vs_font)
            draw.line([(CARD_WIDTH // 2 + vsw // 2 + 16, line_y), (CARD_WIDTH - 80, line_y)],
                      fill=hex_to_rgb(COLORS['divider']), width=1)

            # Opponent team (dimmer, smaller)
            opp_y = vs_y + 30
            opp_font, opp_tw = _fit_team_font(opponent, usable_w, start_size=44)
            opp_bb_real = draw.textbbox((0, 0), opponent.upper(), font=opp_font)
            opp_h_real = opp_bb_real[3] - opp_bb_real[1]
            opp_x = (CARD_WIDTH - opp_tw) // 2
            draw.text((opp_x, opp_y), opponent.upper(),
                      fill=hex_to_rgb(COLORS['text_dim']), font=opp_font)

            # ── Row 6: Probability | Edge | Odds — horizontal strip ──
            strip_y = opp_y + opp_h_real + 20
            # Draw strip background
            draw.rounded_rectangle(
                [(60, strip_y), (CARD_WIDTH - 60, strip_y + 50)],
                radius=12, fill=(26, 38, 52, 200))

            # Win probability (center, large)
            prob_pct = int(win_probability * 100) if win_probability else 0
            prob_text = f"{prob_pct}%"
            prob_font = self._get_font(weight='black', size=32)
            bbox_p = draw.textbbox((0, 0), prob_text, font=prob_font)
            pw = bbox_p[2] - bbox_p[0]
            draw.text(((CARD_WIDTH - pw) // 2, strip_y + 8), prob_text,
                      fill=conf_color, font=prob_font)

            # Edge (left)
            if edge_pct:
                edge_text = f"+{edge_pct:.1f}% EDGE"
                edge_font = self._get_font(weight='bold', size=18)
                draw.text((90, strip_y + 14), edge_text,
                          fill=conf_color, font=edge_font)

            # Odds (right)
            if pick_odds:
                odds_text = f"@ {pick_odds:.2f}"
                odds_font = self._get_font(weight='bold', size=18)
                bbox_o = draw.textbbox((0, 0), odds_text, font=odds_font)
                ow = bbox_o[2] - bbox_o[0]
                draw.text((CARD_WIDTH - ow - 90, strip_y + 14), odds_text,
                          fill=hex_to_rgb(COLORS['text_white']), font=odds_font)

            # Market type label (below strip, small if match_winner)
            if not is_special_market and market_label:
                mkt_small_font = self._get_font(weight='medium', size=13)
                bbox_ms = draw.textbbox((0, 0), market_label, font=mkt_small_font)
                msw = bbox_ms[2] - bbox_ms[0]
                draw.text(((CARD_WIDTH - msw) // 2, strip_y + 54), market_label,
                          fill=hex_to_rgb(COLORS['text_dim']), font=mkt_small_font)

            # ── Row 7: Analysis lines ──
            lines_to_show = analysis_lines[:3] if analysis_lines else []
            if not lines_to_show and key_factor:
                lines_to_show = [key_factor[:80]]
            kf_font = self._get_font(weight='medium', size=15)
            kf_y = strip_y + 70
            for line_text in lines_to_show:
                display = f"• {line_text}"
                # Truncate to fit card width
                while True:
                    bbox_kf = draw.textbbox((0, 0), display, font=kf_font)
                    kfw = bbox_kf[2] - bbox_kf[0]
                    if kfw <= CARD_WIDTH - 160 or len(display) <= 10:
                        break
                    display = display[:len(display) - 4] + "…"
                draw.text(((CARD_WIDTH - kfw) // 2, kf_y), display,
                          fill=hex_to_rgb(COLORS['text_gray']), font=kf_font)
                kf_y += 20

            # ── Bottom accent bar ──
            draw.rectangle([(0, CARD_HEIGHT - 4), (CARD_WIDTH, CARD_HEIGHT)],
                           fill=conf_color)

            self._add_watermark(draw)

            final = base.convert('RGB')
            key = f"{pick_team}{opponent}{win_probability}"
            fname = f"pred_{hashlib.md5(key.encode()).hexdigest()[:8]}.png"
            out_path = OUTPUT_DIR / fname
            final.save(str(out_path), 'PNG', quality=95)
            logger.info(f"🎯 Prediction card generated: {out_path.name}")
            return str(out_path)
        except Exception as e:
            logger.warning(f"⚠️  Prediction card generation failed: {e}")
            return None

    def generate_prediction_result_card(
        self,
        pick_team: str,
        opponent: str,
        result: str,
        win_probability: float = 0.0,
        pick_odds: float = 0.0,
        confidence: str = 'standard',
        event_name: str = '',
        market_type: str = 'match_winner',
        record_wins: int = 0,
        record_losses: int = 0,
    ) -> Optional[str]:
        """
        Generate a WIN or LOSS result card for a settled prediction bet.

        Layout:
          - Full-width result banner: green ✅ WIN or red ❌ LOSS
          - Pick team name (large, centered)
          - "vs {opponent}" dimmed
          - Stats row: odds | probability | edge
          - Running record: "Season: W-L (pct%)"
          - Bottom accent bar in result color
        """
        try:
            is_win = result.lower() == 'win'
            result_color = hex_to_rgb(COLORS['accent_green']) if is_win else hex_to_rgb(COLORS['accent_red'])
            pick_rgb = hex_to_rgb(get_team_color(pick_team))

            # ── RGBA base ──
            base = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT),
                             hex_to_rgb(COLORS['bg_dark']) + (255,))

            # Result-colored glow from top
            glow = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            for y_off in range(200):
                alpha = int(40 * (1 - y_off / 200))
                gd.line([(0, y_off), (CARD_WIDTH, y_off)],
                        fill=result_color + (alpha,))
            base = Image.alpha_composite(base, glow)

            # Scan-line texture
            scan = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
            sd = ImageDraw.Draw(scan)
            for y in range(0, CARD_HEIGHT, 4):
                sd.line([(0, y), (CARD_WIDTH, y)], fill=(0, 0, 0, 30))
            base = Image.alpha_composite(base, scan)

            draw = ImageDraw.Draw(base)

            # ── Row 1: Result banner ──
            banner_text = "W I N" if is_win else "L O S S"
            banner_font = self._get_font(weight='black', size=56)
            bbox = draw.textbbox((0, 0), banner_text, font=banner_font)
            bw = bbox[2] - bbox[0]
            bh = bbox[3] - bbox[1]
            bx = (CARD_WIDTH - bw) // 2
            by = 40
            # Banner background pill
            draw.rounded_rectangle(
                [(bx - 40, by - 10), (bx + bw + 40, by + bh + 16)],
                radius=18, fill=result_color + (200,))
            draw.text((bx, by), banner_text,
                      fill=(255, 255, 255), font=banner_font)

            # ── Row 2: "BET RESULT" subtitle ──
            sub_font = self._get_font(weight='medium', size=16)
            sub_text = "BET RESULT"
            bbox_sub = draw.textbbox((0, 0), sub_text, font=sub_font)
            sw = bbox_sub[2] - bbox_sub[0]
            draw.text(((CARD_WIDTH - sw) // 2, by + bh + 28), sub_text,
                      fill=hex_to_rgb(COLORS['text_dim']), font=sub_font)

            # ── Row 3: Pick team (large, centered) ──
            def _fit_font(name: str, max_w: int, start: int = 52) -> tuple:
                for sz in range(start, 22, -2):
                    f = self._get_font(weight='black', size=sz)
                    bb = draw.textbbox((0, 0), name.upper(), font=f)
                    w = bb[2] - bb[0]
                    if w <= max_w:
                        return f, w
                f = self._get_font(weight='black', size=22)
                bb = draw.textbbox((0, 0), name.upper(), font=f)
                return f, bb[2] - bb[0]

            usable_w = CARD_WIDTH - 160
            pick_font, pick_tw = _fit_font(pick_team, usable_w, 56)
            pick_x = (CARD_WIDTH - pick_tw) // 2
            pick_y = 175
            # Shadow
            draw.text((pick_x + 2, pick_y + 2), pick_team.upper(),
                      fill=(0, 0, 0, 100), font=pick_font)
            draw.text((pick_x, pick_y), pick_team.upper(),
                      fill=hex_to_rgb(COLORS['text_white']), font=pick_font)

            # Colored accent underline
            pick_bb = draw.textbbox((pick_x, pick_y), pick_team.upper(), font=pick_font)
            pick_h = pick_bb[3] - pick_bb[1]
            underline_y = pick_bb[3] + 6
            draw.line(
                [(pick_x, underline_y), (pick_x + pick_tw, underline_y)],
                fill=pick_rgb + (200,), width=3)

            # ── Row 4: "vs OPPONENT" dimmed ──
            vs_text = f"vs {opponent}"
            vs_font = self._get_font(weight='semibold', size=24)
            bbox_vs = draw.textbbox((0, 0), vs_text, font=vs_font)
            vsw = bbox_vs[2] - bbox_vs[0]
            draw.text(((CARD_WIDTH - vsw) // 2, underline_y + 16), vs_text,
                      fill=hex_to_rgb(COLORS['text_dim']), font=vs_font)

            # ── Row 5: Stats strip ──
            strip_y = underline_y + 64
            draw.rounded_rectangle(
                [(100, strip_y), (CARD_WIDTH - 100, strip_y + 50)],
                radius=12, fill=(26, 38, 52, 200))

            # Odds (left)
            if pick_odds:
                odds_text = f"@ {pick_odds:.2f}"
                odds_font = self._get_font(weight='bold', size=22)
                draw.text((140, strip_y + 12), odds_text,
                          fill=hex_to_rgb(COLORS['text_white']), font=odds_font)

            # Probability (center)
            prob_pct = int(win_probability * 100) if win_probability else 0
            if prob_pct > 0:
                prob_text = f"{prob_pct}% PROB"
                prob_font = self._get_font(weight='bold', size=22)
                bbox_p = draw.textbbox((0, 0), prob_text, font=prob_font)
                pw = bbox_p[2] - bbox_p[0]
                draw.text(((CARD_WIDTH - pw) // 2, strip_y + 12), prob_text,
                          fill=result_color, font=prob_font)

            # Market type (right)
            market_label = _format_market_label(market_type)
            mkt_font = self._get_font(weight='medium', size=18)
            bbox_m = draw.textbbox((0, 0), market_label, font=mkt_font)
            mw = bbox_m[2] - bbox_m[0]
            draw.text((CARD_WIDTH - 140 - mw, strip_y + 14),
                      market_label,
                      fill=hex_to_rgb(COLORS['text_gray']), font=mkt_font)

            # ── Row 6: Running record ──
            total = record_wins + record_losses
            pct = (record_wins / total * 100) if total > 0 else 0
            record_text = f"RECORD: {record_wins}W-{record_losses}L"
            if total > 0:
                record_text += f"  ({pct:.0f}% win rate)"
            rec_font = self._get_font(weight='extrabold', size=30)
            bbox_r = draw.textbbox((0, 0), record_text, font=rec_font)
            rw_text = bbox_r[2] - bbox_r[0]
            rec_y = strip_y + 72
            draw.text(((CARD_WIDTH - rw_text) // 2, rec_y), record_text,
                      fill=result_color, font=rec_font)

            # Event name (small, below record)
            if event_name:
                ev_font = self._get_font(weight='medium', size=16)
                ev_text = event_name.upper()
                if len(ev_text) > 50:
                    ev_text = ev_text[:47] + "..."
                bbox_ev = draw.textbbox((0, 0), ev_text, font=ev_font)
                evw = bbox_ev[2] - bbox_ev[0]
                draw.text(((CARD_WIDTH - evw) // 2, rec_y + 40),
                          ev_text,
                          fill=hex_to_rgb(COLORS['text_gray']), font=ev_font)

            # ── Bottom accent bar ──
            draw.rectangle([(0, CARD_HEIGHT - 6), (CARD_WIDTH, CARD_HEIGHT)],
                           fill=result_color)

            self._add_watermark(draw)

            final = base.convert('RGB')
            tag = "win" if is_win else "loss"
            key = f"{pick_team}{opponent}{result}"
            fname = f"res_{tag}_{hashlib.md5(key.encode()).hexdigest()[:8]}.png"
            out_path = OUTPUT_DIR / fname
            final.save(str(out_path), 'PNG', quality=95)
            logger.info(f"{'✅' if is_win else '❌'} Result card generated: {out_path.name}")
            return str(out_path)
        except Exception as e:
            logger.warning(f"⚠️  Result card generation failed: {e}")
            return None

    def generate_multi_team_card(self, teams: List[str],
                                  headline: str = '',
                                  event_name: str = None) -> Optional[str]:
        """
        Generate a card featuring multiple teams (3+ or 2 non-vs).
        Shows each team name in their brand color, stacked vertically
        with an optional headline (e.g. "Playoff Qualifiers").

        Args:
            teams: List of team names (e.g. ["3DMAX", "The MongolZ", "MIBR"])
            headline: Optional top text (e.g. "INTO THE PLAYOFFS")
            event_name: Tournament name

        Returns: path to generated image or None
        """
        if not teams:
            return None
        try:
            img, draw = self._create_base_card()

            # Event name at top
            if event_name:
                event_font = self._get_font(bold=False, size=22)
                bbox = draw.textbbox((0, 0), event_name, font=event_font)
                x = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
                draw.text((x, 25), event_name, fill=hex_to_rgb(COLORS['text_gray']), font=event_font)

            # Headline
            start_y = 70 if event_name else 40
            if headline:
                hl_font = self._get_font(bold=True, size=36)
                bbox = draw.textbbox((0, 0), headline.upper(), font=hl_font)
                x = (CARD_WIDTH - (bbox[2] - bbox[0])) // 2
                draw.text((x, start_y), headline.upper(),
                          fill=hex_to_rgb(COLORS['accent_yellow']), font=hl_font)
                start_y += 60

            # Divider below headline
            draw.rectangle([(60, start_y), (CARD_WIDTH - 60, start_y + 2)],
                           fill=hex_to_rgb(COLORS['divider']))
            start_y += 20

            # Compute font size based on number of teams so they all fit
            num_teams = len(teams)
            available_height = CARD_HEIGHT - start_y - 50  # leave room for watermark
            team_font_size = min(60, max(32, available_height // num_teams - 10))
            team_font = self._get_font(bold=True, size=team_font_size)
            spacing = available_height // num_teams

            for i, team_name in enumerate(teams[:6]):  # cap at 6 teams
                color = get_team_color(team_name)
                y = start_y + i * spacing + (spacing - team_font_size) // 2

                # Team name centered
                display_name = team_name.upper()
                bbox = draw.textbbox((0, 0), display_name, font=team_font)
                text_width = bbox[2] - bbox[0]
                x = (CARD_WIDTH - text_width) // 2

                # Colored accent bar to the left of text
                bar_height = team_font_size + 4
                draw.rectangle([(x - 20, y), (x - 12, y + bar_height)],
                               fill=hex_to_rgb(color))

                draw.text((x, y), display_name, fill=hex_to_rgb(color), font=team_font)

            self._add_watermark(draw)

            key = ''.join(teams[:6])
            fname = f"multi_{hashlib.md5(key.encode()).hexdigest()[:8]}.png"
            out_path = OUTPUT_DIR / fname
            img.save(str(out_path), 'PNG', quality=95)
            logger.info(f"🎨 Multi-team card generated: {out_path.name}")
            return str(out_path)
        except Exception as e:
            logger.warning(f"⚠️  Multi-team card generation failed: {e}")
            return None

    def generate_headline_card(
        self,
        headline: str,
        tweet_text: str = '',
        category: str = '',
        teams: list = None,
        event_name: str = '',
    ) -> Optional[str]:
        """
        Universal fallback card for any tweet that has no specific card type.

        Layout (1200x675):
          - Dark CS2 background with team-colored accent glow (or gold)
          - Top: Category pill (NEWS / HOT TAKE / ANALYSIS / etc.)
          - Center: Headline text, word-wrapped, auto-scaled
          - Bottom-left: Event name (if available)
          - Accent bar at bottom in team/category color
          - Watermark
        """
        try:
            # Pick accent color from teams or category
            accent = hex_to_rgb(COLORS['accent_yellow'])
            if teams:
                accent = hex_to_rgb(get_team_color(teams[0]))

            # ── RGBA base ──
            base = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT),
                             hex_to_rgb(COLORS['bg_dark']) + (255,))

            # Accent glow from left
            glow = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            for x_off in range(350):
                alpha = int(25 * (1 - x_off / 350))
                gd.line([(x_off, 0), (x_off, CARD_HEIGHT)],
                        fill=accent + (alpha,))
            base = Image.alpha_composite(base, glow)

            # Scan-line texture
            scan = Image.new('RGBA', (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
            sd = ImageDraw.Draw(scan)
            for y in range(0, CARD_HEIGHT, 4):
                sd.line([(0, y), (CARD_WIDTH, y)], fill=(0, 0, 0, 20))
            base = Image.alpha_composite(base, scan)

            draw = ImageDraw.Draw(base)

            # ── Category pill ──
            _CAT_LABELS = {
                'match_result': 'MATCH RESULT',
                'match_preview': 'MATCH PREVIEW',
                'match_highlight': 'HIGHLIGHT',
                'roster_change': 'ROSTER MOVE',
                'cs2_update': 'CS2 UPDATE',
                'cs2': 'CS2 NEWS',
                'analysis': 'ANALYSIS',
                'tournament': 'TOURNAMENT',
            }
            cat_label = _CAT_LABELS.get(category, 'CS2 NEWS')
            cat_font = self._get_font(weight='extrabold', size=16)
            bbox_c = draw.textbbox((0, 0), cat_label, font=cat_font)
            cw = bbox_c[2] - bbox_c[0]
            ch = bbox_c[3] - bbox_c[1]
            cx = (CARD_WIDTH - cw) // 2
            cy = 30
            draw.rounded_rectangle(
                [(cx - 18, cy - 6), (cx + cw + 18, cy + ch + 10)],
                radius=12, fill=accent + (200,))
            # Dark text on bright pill for readability
            draw.text((cx, cy), cat_label,
                      fill=(15, 20, 30), font=cat_font)

            # ── Headline text — auto-scale and word-wrap ──
            # Use the headline (event headline) or fall back to tweet text
            display_text = headline.strip() if headline else tweet_text.strip()
            # Remove URLs
            import re as _re
            display_text = _re.sub(r'https?://\S+', '', display_text).strip()
            # Remove @mentions at the start
            display_text = _re.sub(r'^@\S+\s*', '', display_text).strip()
            if not display_text:
                display_text = tweet_text.strip()[:120]

            # Truncate very long text
            if len(display_text) > 200:
                display_text = display_text[:197] + "..."

            usable_w = CARD_WIDTH - 160  # 80px margin each side
            max_lines = 6

            # Find the best font size that fits
            best_font = None
            best_lines = []
            for sz in range(42, 20, -2):
                test_font = self._get_font(weight='bold', size=sz)
                lines = self._text_wrap(display_text, test_font, usable_w, draw)
                if len(lines) <= max_lines:
                    best_font = test_font
                    best_lines = lines
                    break
            if not best_font:
                best_font = self._get_font(weight='bold', size=20)
                best_lines = self._text_wrap(display_text, best_font, usable_w, draw)[:max_lines]

            # Measure total text height
            line_spacing = 8
            total_h = 0
            for line in best_lines:
                bb = draw.textbbox((0, 0), line, font=best_font)
                total_h += (bb[3] - bb[1]) + line_spacing

            # Center block vertically in available space (below pill, above footer)
            top_zone = cy + ch + 30  # below category pill
            bottom_zone = CARD_HEIGHT - 80  # above footer area
            available_h = bottom_zone - top_zone
            text_y = top_zone + max(0, (available_h - total_h) // 2)

            for line in best_lines:
                bb = draw.textbbox((0, 0), line, font=best_font)
                lw = bb[2] - bb[0]
                lh = bb[3] - bb[1]
                lx = (CARD_WIDTH - lw) // 2
                # Shadow
                draw.text((lx + 2, text_y + 2), line,
                          fill=(0, 0, 0, 80), font=best_font)
                draw.text((lx, text_y), line,
                          fill=hex_to_rgb(COLORS['text_white']), font=best_font)
                text_y += lh + line_spacing

            # ── Event name at bottom ──
            if event_name:
                ev_font = self._get_font(weight='medium', size=16)
                ev_text = event_name.upper()
                if len(ev_text) > 55:
                    ev_text = ev_text[:52] + "..."
                bbox_ev = draw.textbbox((0, 0), ev_text, font=ev_font)
                evw = bbox_ev[2] - bbox_ev[0]
                draw.text(((CARD_WIDTH - evw) // 2, CARD_HEIGHT - 50),
                          ev_text,
                          fill=hex_to_rgb(COLORS['text_gray']), font=ev_font)

            # ── Team names as subtle accent (if any) ──
            if teams and len(teams) >= 2:
                t_font = self._get_font(weight='semibold', size=14)
                vs_label = f"{teams[0]}  vs  {teams[1]}".upper()
                bbox_t = draw.textbbox((0, 0), vs_label, font=t_font)
                tw_size = bbox_t[2] - bbox_t[0]
                draw.text(((CARD_WIDTH - tw_size) // 2, CARD_HEIGHT - 70),
                          vs_label,
                          fill=hex_to_rgb(COLORS['text_dim']), font=t_font)

            # ── Bottom accent bar ──
            draw.rectangle([(0, CARD_HEIGHT - 4), (CARD_WIDTH, CARD_HEIGHT)],
                           fill=accent)

            self._add_watermark(draw)

            final = base.convert('RGB')
            key = f"{headline}{tweet_text}"[:120]
            fname = f"hl_{hashlib.md5(key.encode()).hexdigest()[:8]}.png"
            out_path = OUTPUT_DIR / fname
            final.save(str(out_path), 'PNG', quality=95)
            logger.info(f"📰 Headline card generated: {out_path.name}")
            return str(out_path)
        except Exception as e:
            logger.warning(f"⚠️  Headline card generation failed: {e}")
            return None


# Singleton
_meme_gen = None

def get_meme_generator() -> MemeGenerator:
    global _meme_gen
    if _meme_gen is None:
        _meme_gen = MemeGenerator()
    return _meme_gen
