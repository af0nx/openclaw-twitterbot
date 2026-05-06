#!/usr/bin/env python3
"""
HLTV Monitor - Twitter Bot Pipeline V2
Monitors HLTV.org for CS2 match results, breaking news, and community sentiment.
Scrapes match comments to learn memes, inside jokes, and team reputation.
Uses Scrapling StealthySession with Cloudflare bypass.

MiroFish swarm intelligence integration (github.com/666ghj/MiroFish):
  Community comment analysis → swarm consensus confidence score.
  When the HLTV comment section agrees on something (even stupidly),
  that's signal. When it's split 50/50, that's noise AND content.
"""

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from typing import List, Dict, Any, Optional
import os

from scrapling.fetchers import StealthySession
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
from utils.db_utils import ensure_db_connection

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class HLTVMonitor:
    """Monitor HLTV for CS2 esports news and match results"""

    _RESULT_NEWS_PATTERNS = [
        re.compile(
            r"^\s*(?P<winner>[A-Z0-9][A-Za-z0-9&.'\- ]{0,50}?)\s+"
            r"(?:beat|beats|defeat|defeats|defeated|down|downs|sweep|sweeps|swept|"
            r"edge|edges|edged|outlast|outlasts|upset|upsets|stun|stuns)\s+"
            r"(?P<loser>[A-Z0-9][A-Za-z0-9&.'\- ]{0,50}?)\s+"
            r"(?P<score1>[0-9])\s*[-:]\s*(?P<score2>[0-9])"
            r"(?:\s+(?:to|in|at|for)\s+(?P<context>.+))?\s*$",
            re.IGNORECASE,
        )
    ]
    
    def __init__(self):
        self.db_conn = None
        self.session = None
        self.seen_match_ids = set()
        # Dedicated single-thread executor: Playwright greenlets must always run
        # in the same OS thread they were created in.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hltv_pw')
        
    def connect_db(self):
        """Establish PostgreSQL connection"""
        try:
            self.db_conn = ensure_db_connection(self.db_conn)
            logger.info("✅ Connected to PostgreSQL")
            
            # Load recent match IDs to avoid duplicates
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT metadata->>'hltv_match_id'
                    FROM twitter_bot.events
                    WHERE source = 'hltv'
                    AND created_at > NOW() - INTERVAL '3 days'
                    AND metadata->>'hltv_match_id' IS NOT NULL
                """)
                self.seen_match_ids = {row[0] for row in cur.fetchall() if row[0]}
                logger.info(f"Loaded {len(self.seen_match_ids)} seen match IDs")
                
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise

    def _ensure_db(self):
        self.db_conn = ensure_db_connection(self.db_conn)
    
    async def scrape_matches(self) -> List[Dict[str, Any]]:
        """Scrape recent match results from HLTV"""
        try:
            if not self.session:
                # StealthySession with Cloudflare solving
                self.session = StealthySession(
                    solve_cloudflare=True,
                    headless=True
                )
                await asyncio.get_event_loop().run_in_executor(self._executor, self.session.start)
            
            url = 'https://www.hltv.org/results'
            
            page = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                partial(self.session.fetch, url, solve_cloudflare=True, network_idle=True)
            )
            
            matches = []
            
            # Parse match result containers
            # NOTE: Selectors may change - auto-healing via scrapling_medic.sh
            result_containers = page.css('.result-con')
            
            # T1 event names — only tweet about matches from these events
            T1_EVENTS = {
                'esl pro league', 'blast', 'iem', 'pgl', 'major',
                'esl challenger', 'betboom', 'perfect world',
                'yalla compass', 'thunderpick', 'skyesports',
                'esea premier', 'ccgs', 'cct', 'roobet cup',
            }
            
            for container in result_containers[:25]:  # Check more matches to find T1
                try:
                    # Extract match ID from URL
                    a_elems = container.css('a.a-reset')
                    match_link = a_elems[0].attrib.get('href') if a_elems else None
                    match_id = match_link.split('/')[-2] if match_link else None
                    
                    if not match_id or match_id in self.seen_match_ids:
                        continue
                    
                    # Extract team names and scores
                    team1_elems = container.css('.team1')
                    team2_elems = container.css('.team2')
                    team1_elem = team1_elems[0] if team1_elems else None
                    team2_elem = team2_elems[0] if team2_elems else None
                    
                    t1_name_elems = team1_elem.css('.team') if team1_elem else []
                    t2_name_elems = team2_elem.css('.team') if team2_elem else []
                    team1_name = t1_name_elems[0].text.strip() if t1_name_elems else 'Unknown'
                    team2_name = t2_name_elems[0].text.strip() if t2_name_elems else 'Unknown'
                    
                    t1_score_elems = team1_elem.css('.score') if team1_elem else []
                    t2_score_elems = team2_elem.css('.score') if team2_elem else []
                    team1_score = t1_score_elems[0].text.strip() if t1_score_elems else '0'
                    team2_score = t2_score_elems[0].text.strip() if t2_score_elems else '0'
                    
                    # Extract event name
                    event_elems = container.css('.event-name')
                    event_name = event_elems[0].text.strip() if event_elems else 'Unknown Event'
                    
                    # Only tweet about T1 events (skip random T3/regional matches)
                    event_lower = event_name.lower()
                    if not any(t1 in event_lower for t1 in T1_EVENTS):
                        continue
                    
                    # Determine winner
                    try:
                        score1 = int(team1_score)
                        score2 = int(team2_score)
                        # Skip 0-0 matches (upcoming/live/cancelled — not real results)
                        if score1 == 0 and score2 == 0:
                            continue
                        winner = team1_name if score1 > score2 else team2_name
                    except:
                        winner = "Unknown"
                    
                    matches.append({
                        'match_id': match_id,
                        'team1': team1_name,
                        'team2': team2_name,
                        'score1': team1_score,
                        'score2': team2_score,
                        'winner': winner,
                        'event': event_name,
                        'url': f'https://www.hltv.org{match_link}'
                    })
                    
                    self.seen_match_ids.add(match_id)
                    
                except Exception as e:
                    logger.warning(f"⚠️  Failed to parse match result: {e}")
                    continue
            
            if matches:
                logger.info(f"✅ HLTV: Scraped {len(matches)} new match results")
            
            return matches
            
        except Exception as e:
            logger.error(f"❌ Failed to scrape HLTV matches: {e}")
            return []
    
    async def scrape_news(self) -> List[Dict[str, Any]]:
        """Scrape breaking news from HLTV"""
        try:
            if not self.session:
                self.session = StealthySession(solve_cloudflare=True, headless=True)
                await asyncio.get_event_loop().run_in_executor(self._executor, self.session.start)
            
            url = 'https://www.hltv.org/'
            
            page = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                partial(self.session.fetch, url, solve_cloudflare=True, network_idle=True)
            )
            
            news_items = []
            
            # Parse news articles
            articles = page.css('.article')
            
            for article in articles[:5]:  # Latest 5 articles
                try:
                    headline_els = article.css('.newstext')
                    headline = headline_els[0].text.strip() if headline_els else ''
                    link_els = article.css('a')
                    link = link_els[0].attrib.get('href') if link_els else None
                    article_id = link.split('/')[-2] if link else None
                    
                    if not article_id or f"news_{article_id}" in self.seen_match_ids:
                        continue
                    
                    # Classify urgency
                    urgency = 'breaking' if any(kw in headline.lower() for kw in [
                        'breaking', 'confirm', 'sign', 'leave', 'join', 'announce'
                    ]) else 'normal'
                    
                    news_items.append({
                        'article_id': article_id,
                        'headline': headline,
                        'url': f'https://www.hltv.org{link}',
                        'urgency': urgency
                    })
                    
                    self.seen_match_ids.add(f"news_{article_id}")
                    
                except Exception as e:
                    logger.warning(f"⚠️  Failed to parse news article: {e}")
                    continue
            
            if news_items:
                logger.info(f"✅ HLTV: Scraped {len(news_items)} news articles")
            
            return news_items
            
        except Exception as e:
            logger.error(f"❌ Failed to scrape HLTV news: {e}")
            return []
    
    def extract_match_context(self, page_html: str) -> Dict[str, Any]:
        """
        Extract tournament context from an HLTV match page HTML.
        Returns bracket position, format, stakes, map vetoes etc.
        This powers the "1 MAP LOSS AWAY from MISSING the Major" style tweets.
        """
        ctx: Dict[str, Any] = {}
        text_lower = page_html.lower()

        # Match format (bo1, bo3, bo5)
        for fmt in ['bo5', 'bo3', 'bo1']:
            if fmt in text_lower:
                ctx['format'] = fmt.upper()
                break

        # Bracket / stage labels from the page
        bracket_patterns = [
            (r'(upper bracket[^<]{0,60})', 'upper_bracket'),
            (r'(lower bracket[^<]{0,60})', 'lower_bracket'),
            (r'(grand final[^<]{0,40})', 'grand_final'),
            (r'(semi-?final[^<]{0,40})', 'semifinal'),
            (r'(quarter-?final[^<]{0,40})', 'quarterfinal'),
            (r'(elimination[^<]{0,40})', 'elimination'),
            (r'(decider[^<]{0,40})', 'decider'),
            (r'(group stage[^<]{0,40})', 'group_stage'),
            (r'(consolidation[^<]{0,40})', 'consolidation'),
            (r'(winners match|winners final)[^<]{0,40}', 'winners_match'),
            (r'(losers match|losers final)[^<]{0,40}', 'losers_match'),
        ]

        found_stages = []
        for pattern, label in bracket_patterns:
            matches = re.findall(pattern, text_lower)
            if matches:
                for m in matches[:2]:
                    clean = re.sub(r'\s+', ' ', m).strip()
                    if len(clean) > 5:
                        found_stages.append(clean)
                if 'stage_type' not in ctx:
                    ctx['stage_type'] = label

        if found_stages:
            ctx['stage_label'] = found_stages[0]

        # Map vetoes
        picked_maps = re.findall(r'picked"?\s*data-mapname="([^"]+)"', text_lower)
        removed_maps = re.findall(r'removed"?\s*data-mapname="([^"]+)"', text_lower)
        if picked_maps:
            ctx['maps_picked'] = picked_maps
        if removed_maps:
            ctx['maps_banned'] = removed_maps

        # Editorial headlines from the match page
        headlines = re.findall(r'newstext[^>]*>([^<]+)', page_html)
        editorial = [h.strip() for h in headlines[:3] if h.strip() and len(h.strip()) > 10]
        if editorial:
            ctx['editorial_headlines'] = editorial

        # Stakes context — key phrases about elimination, qualification etc.
        # Only match from editorial/news blocks, not player stats
        stakes_phrases = re.findall(
            r'(?:newstext|article|headline|standard-box)[^>]*>[^<]*'
            r'((?:eliminated|elimination|knocked out|drop(?:ped)? to lower|advance[ds]? to|'
            r'qualif(?:y|ied)|playoff|'
            r'champions stage|legends stage|challengers stage|contenders|'
            r'out of the tournament|miss(?:ing)? the major|'
            r'one (?:map|game|loss|win) (?:away|from)|must.?win|do.?or.?die|'
            r'last chance|fighting for)[^<]{0,120})',
            text_lower
        )
        if stakes_phrases:
            ctx['stakes_context'] = [re.sub(r'\s+', ' ', s).strip() for s in stakes_phrases[:3]]
        else:
            # Fallback: search entire page but with stricter multi-word patterns
            stakes_fallback = re.findall(
                r'((?:drop(?:ped)? to (?:the )?lower bracket|'
                r'eliminated from|knocked out of|advance[ds]? to (?:the )?(?:playoff|upper|grand final)|'
                r'miss(?:ing)? the major|out of the (?:tournament|major|event)|'
                r'one (?:loss|map|win) (?:away|from)|must.?win (?:match|game|series)|'
                r'fighting for (?:survival|their lives|a spot))[^.<]{0,80}[.]?)',
                text_lower
            )
            if stakes_fallback:
                ctx['stakes_context'] = [re.sub(r'\s+', ' ', s).strip() for s in stakes_fallback[:3]]

        return ctx

    async def scrape_match_page(self, match_url: str) -> tuple:
        """Scrape match page and return (comments, match_context)."""
        try:
            if not self.session:
                self.session = StealthySession(solve_cloudflare=True, headless=True)
                await asyncio.get_event_loop().run_in_executor(self._executor, self.session.start)

            page = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                partial(self.session.fetch, match_url, solve_cloudflare=True, network_idle=True)
            )

            page_html = page.html if hasattr(page, 'html') else str(page)

            # Extract tournament context from full page HTML
            match_context = self.extract_match_context(page_html)

            # Extract player stats (ratings, KD, ADR) from JS-rendered HTML
            player_stats = self.extract_player_stats(page_html)
            if player_stats:
                match_context['player_stats'] = player_stats
                # Find MVP (highest rated player)
                best = max(player_stats, key=lambda p: float(p.get('rating', 0)))
                match_context['mvp'] = best['name']
                match_context['mvp_rating'] = best.get('rating', '')

            # Extract comments
            comments = []
            comment_elements = page.css('.c-header')
            if not comment_elements:
                comment_elements = page.css('.matchComment') or page.css('.commentText') or page.css('.comment-text')

            for el in comment_elements[:20]:
                try:
                    text = el.text.strip()
                    if text and len(text) > 5 and len(text) < 300:
                        comments.append(text)
                except Exception:
                    continue

            logger.info(f"💬 HLTV match scraped: {len(comments)} comments, context={list(match_context.keys())} from {match_url}")
            return comments, match_context

        except Exception as e:
            logger.warning(f"⚠️  Failed to scrape match page {match_url}: {e}")
            return [], {}

    def extract_player_stats(self, page_html: str) -> List[Dict[str, Any]]:
        """
        Extract player performance stats from HLTV match page HTML.
        Returns list of dicts: [{'name': 'donk', 'rating': '1.45', 'kd_diff': '+15', 'adr': '89.2', 'team': 'Spirit'}, ...]
        Works on JS-rendered HTML (from StealthySession).
        """
        players = []
        try:
            # HLTV match page has stats tables with player rows
            # Pattern: player link + rating value in same row context
            # The stats table has class "stats-table" or "totalstats" or similar

            # Try to extract from match stats table
            # Each player row: <a href="/player/ID/name">name</a> ... <td class="rating">1.45</td>
            player_rows = re.findall(
                r'<tr[^>]*>(?:(?!</tr>).)*?'
                r'/player/(\d+)/([a-zA-Z0-9_-]+)'
                r'(?:(?!</tr>).)*?'
                r'</tr>',
                page_html, re.DOTALL
            )

            # Also find rating values near player names
            # More robust: find all player+rating pairs
            stat_blocks = re.findall(
                r'href="/player/\d+/([^"]+)"[^>]*>[^<]*</a>'
                r'(?:(?!</tr>).)*?'
                r'(?:class="[^"]*rating[^"]*"[^>]*>|rating[^>]*>)\s*([0-9]\.[0-9]{2})',
                page_html, re.DOTALL | re.IGNORECASE
            )

            if stat_blocks:
                seen = set()
                for name, rating in stat_blocks:
                    name_clean = name.strip().lower()
                    if name_clean not in seen and len(name_clean) > 1:
                        seen.add(name_clean)
                        players.append({
                            'name': name_clean,
                            'rating': rating,
                        })

            # Try broader extraction: find KD diff, ADR from table cells near player
            for p in players:
                # Look for KD diff near the player name
                kd_match = re.search(
                    re.escape(p['name']) + r'(?:(?!</tr>).)*?'
                    r'(?:class="[^"]*kd[^"]*"[^>]*>|kd-diff[^>]*>)\s*([+-]?\d+)',
                    page_html, re.DOTALL | re.IGNORECASE
                )
                if kd_match:
                    p['kd_diff'] = kd_match.group(1)

                adr_match = re.search(
                    re.escape(p['name']) + r'(?:(?!</tr>).)*?'
                    r'(?:class="[^"]*adr[^"]*"[^>]*>)\s*([0-9]+\.?[0-9]*)',
                    page_html, re.DOTALL | re.IGNORECASE
                )
                if adr_match:
                    p['adr'] = adr_match.group(1)

            if players:
                logger.info(f"📊 Extracted stats for {len(players)} players: {[p['name'] for p in players]}")

        except Exception as e:
            logger.debug(f"⚠️  Player stats extraction: {e}")

        return players

    async def scrape_match_comments(self, match_url: str) -> List[str]:
        """
        Scrape top community comments from a match page.
        This is the key source for CS2 memes, team reputation, and community vibes.
        """
        try:
            if not self.session:
                self.session = StealthySession(solve_cloudflare=True, headless=True)
                await asyncio.get_event_loop().run_in_executor(self._executor, self.session.start)

            page = await asyncio.get_event_loop().run_in_executor(
                self._executor,
                partial(self.session.fetch, match_url, solve_cloudflare=True, network_idle=True)
            )

            comments = []

            # HLTV comment containers have class 'c-header' for the text portion
            comment_elements = page.css('.c-header')
            if not comment_elements:
                # Fallback selectors
                comment_elements = page.css('.matchComment') or page.css('.commentText') or page.css('.comment-text')

            for el in comment_elements[:20]:
                try:
                    text = el.text.strip()
                    if text and len(text) > 5 and len(text) < 300:
                        comments.append(text)
                except Exception:
                    continue

            logger.info(f"💬 HLTV comments scraped: {len(comments)} from {match_url}")
            return comments

        except Exception as e:
            logger.warning(f"⚠️  Failed to scrape match comments from {match_url}: {e}")
            return []

    def extract_community_vibe(self, comments: List[str], team1: str, team2: str) -> Dict[str, Any]:
        """
        Analyse scraped comments to extract:
        - Top recurring memes / phrases
        - Community sentiment toward each team
        - MiroFish swarm consensus confidence score
        - Key inside jokes found in the comment section

        Inspired by MiroFish swarm intelligence engine:
        github.com/666ghj/MiroFish — "Predicting Anything with Swarm Intelligence"
        Here: the HLTV comment section IS the swarm. It's dumb, it's loud, it's right ~60% of the time.
        """
        if not comments:
            return {'memes': [], 'team_sentiment': {}, 'top_comments': [], 'swarm_confidence': 0.5}

        full_text = ' '.join(comments).lower()

        # Extended CS2 community memes / phrases
        cs2_memes = [
            # Classic HLTV canon
            ('ez4ence', 'EZ4ENCE'),
            ('ez for', 'EZ for'),
            ('cry is free', 'cry is free'),
            ('esl please', 'ESL please'),
            ('fallen god', 'FalleN god'),
            ('liquid major', 'Liquid major curse'),
            ('liquid curse', 'Liquid curse'),
            ('navi > all', 'NaVi > all'),
            ('s1mple best', 's1mple GOAT'),
            ('s1mple goat', 's1mple GOAT'),
            ('pita strat', 'pita strat'),
            ('nuke is ct sided', 'Nuke CT-sided'),
            ('eco round', 'eco round'),
            ('1.6 was better', '1.6 was better'),
            ('csgo better than cs2', 'CSGO > CS2'),
            ('major incoming', 'Major incoming'),
            ('rip bozo', 'rip bozo'),
            ('kekw', 'KEKW'),
            ('pepehands', 'PepeHands'),
            # Modern CS2 forum energy
            ('literally unplayable', 'literally unplayable'),
            ('valve fix', 'VALVE FIX'),
            ('patch notes', 'patch notes pls'),
            ('prime was better', 'prime was better'),
            ('online doesn\'t count', 'online doesn\'t count'),
            ('lan monster', 'LAN monster'),
            ('overrated', 'overrated'),
            ('top 1 world', 'top 1 world'),
            ('not even close', 'not even close'),
            ('throw speed', 'throw speed%'),
            ('certified throwers', 'certified throwers'),
            ('tactical timeout', 'tactical timeout abuse'),
            ('buy a rifle', 'buy a rifle'),
            ('awp abuse', 'AWP abuse'),
            ('deag diff', 'deag diff'),
            ('aim diff', 'aim diff'),
            ('iq diff', 'IQ diff'),
            ('igl diff', 'IGL diff'),
            ('who asked', 'who asked'),
            ('nobody asked', 'nobody asked'),
            ('bait and switch', 'baited'),
            ('insane players', 'these players are insane'),
            ('expected', 'expected'),
            ('didn\'t expect', 'didn\'t expect'),
            ('tier 3 at best', 'tier 3 at best'),
            ('group stage exit', 'group stage exit'),
            ('always choke', 'always choke'),
            ('hltv journalist', 'HLTV journalism'),
            ('match fixing', 'match fixing???'),
            ('100 euros on', '100 euros on'),
            ('lost my skins', 'lost my skins'),
            ('gg ez', 'GG EZ'),
            ('not even', 'not even a contest'),
        ]

        found_memes = []
        for keyword, label in cs2_memes:
            if keyword in full_text:
                found_memes.append(label)

        # Sentiment signals per team
        def team_sentiment(team_name: str) -> str:
            name = team_name.lower()
            pos = sum(1 for c in comments if name in c.lower() and any(
                w in c.lower() for w in [
                    'ez', 'best', 'goat', 'king', 'god', 'win', 'crush',
                    'destroy', 'dominate', 'insane', 'clean', 'clap', 'stomp'
                ]
            ))
            neg = sum(1 for c in comments if name in c.lower() and any(
                w in c.lower() for w in [
                    'trash', 'bot', 'overrated', 'throw', 'lose', 'scam',
                    'dead', 'washed', 'choke', 'rip', 'joke', 'bait', 'tier 3'
                ]
            ))
            if pos > neg + 1:
                return 'hyped'
            elif neg > pos + 1:
                return 'doubted'
            return 'neutral'

        team_sentiments = {
            team1: team_sentiment(team1),
            team2: team_sentiment(team2),
        }

        # MiroFish swarm consensus confidence score
        # Inspired by: github.com/666ghj/MiroFish
        # Logic: if comments all trend the same direction → high confidence (swarm converged)
        #        if split chaotically → low confidence (swarm confused)
        #        The HLTV swarm is dumb but directionally correct ~60% of the time.
        swarm_confidence = self._mirofish_swarm_confidence(comments, team1, team2)

        # Select most interesting comments (short, punchy)
        top_comments = sorted(
            [c for c in comments if 10 <= len(c) <= 120],
            key=lambda c: len(c)
        )[:5]
        return {
            'memes': found_memes[:10],
            'team_sentiment': team_sentiments,
            'top_comments': top_comments,
            'swarm_confidence': swarm_confidence,
        }

    def _mirofish_swarm_confidence(self, comments: List[str], team1: str, team2: str) -> float:
        """
        Compute a MiroFish-style swarm consensus confidence score (0.0 - 1.0).

        Concept: github.com/666ghj/MiroFish — Swarm Intelligence Engine.
        The HLTV comment section is a swarm. Dumb individually, occasionally wise collectively.
        When the swarm converges on one team → confidence high.
        When the swarm is split / chaotic → confidence low.

        Score interpretation (used in content_generator.py prompts):
          0.0-0.3  comment section is a warzone, 50/50 chaos
          0.3-0.6  mixed signals, rats going in circles  
          0.6-0.8  swarm leaning heavily one way
          0.8-1.0  HLTV has spoken, result was obvious to the rats
        """
        if len(comments) < 3:
            return 0.5  # Not enough signal

        t1 = team1.lower()
        t2 = team2.lower()

        pos_words = ['ez', 'best', 'goat', 'win', 'crush', 'destroy', 'insane', 'stomp', 'clap', 'clean']
        neg_words = ['trash', 'bot', 'overrated', 'throw', 'lose', 'choke', 'washed', 'rip', 'tier 3']

        votes_t1 = 0
        votes_t2 = 0

        for c in comments:
            cl = c.lower()
            has_t1 = t1 in cl
            has_t2 = t2 in cl
            has_pos = any(w in cl for w in pos_words)
            has_neg = any(w in cl for w in neg_words)

            if has_t1 and has_pos and not has_t2:
                votes_t1 += 1
            elif has_t1 and has_neg and not has_t2:
                votes_t2 += 0.5  # implicitly backing t2
            if has_t2 and has_pos and not has_t1:
                votes_t2 += 1
            elif has_t2 and has_neg and not has_t1:
                votes_t1 += 0.5

        total = votes_t1 + votes_t2
        if total < 1:
            return 0.5  # swarm is talking about other things entirely (valid chaos)

        # Imbalance ratio — how one-sided is the swarm?
        majority = max(votes_t1, votes_t2)
        ratio = majority / total  # 0.5 = perfectly split, 1.0 = unanimous

        # Scale to confidence: 0.5 ratio → 0.1 confidence, 1.0 ratio → 0.95 confidence
        confidence = min(0.95, max(0.05, (ratio - 0.5) * 1.8 + 0.1))
        return round(confidence, 2)

    def store_matches(self, matches: List[Dict[str, Any]]):
        """Store match results in database, including community vibe metadata"""
        if not matches:
            return
        
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                for match in matches:
                    headline = f"{match['team1']} {match['score1']}-{match['score2']} {match['team2']}"
                    
                    # Build a richer content blob that the content generator can use
                    vibe = match.get('community_vibe', {})
                    memes_str = ', '.join(vibe.get('memes', [])) if vibe.get('memes') else ''
                    top_comments = vibe.get('top_comments', [])
                    team_sentiment = vibe.get('team_sentiment', {})

                    winner_sentiment = team_sentiment.get(match['winner'], 'neutral')
                    content_parts = [
                        f"{match['winner']} defeats {match['team1'] if match['winner'] == match['team2'] else match['team2']} in {match['event']}."
                    ]

                    # Tournament context — bracket position, stakes, format
                    mctx = match.get('match_context', {})
                    if mctx.get('stage_label'):
                        content_parts.append(f"Match type: {mctx['stage_label']}.")
                    if mctx.get('format'):
                        content_parts.append(f"Format: {mctx['format']}.")
                    if mctx.get('stakes_context'):
                        content_parts.append(f"Stakes: {' '.join(mctx['stakes_context'][:2])}")
                    if mctx.get('editorial_headlines'):
                        content_parts.append(f"Context: {mctx['editorial_headlines'][0]}")

                    if memes_str:
                        content_parts.append(f"Community memes spotted: {memes_str}.")
                    if top_comments:
                        content_parts.append(f"Community says: \"{top_comments[0]}\"")
                    if winner_sentiment != 'neutral':
                        content_parts.append(f"Crowd is {winner_sentiment} on {match['winner']}.")
                    swarm_score = vibe.get('swarm_confidence', 0.5)
                    if swarm_score >= 0.75:
                        content_parts.append(f"MiroFish swarm confidence: {swarm_score:.2f} (HLTV had this called).")
                    elif swarm_score <= 0.3:
                        content_parts.append(f"MiroFish swarm confidence: {swarm_score:.2f} (comment section in full chaos).")

                    content = ' '.join(content_parts)

                    cur.execute("""
                        INSERT INTO twitter_bot.events
                        (headline, content, source, source_url, category, urgency, status, metadata)
                        VALUES (%s, %s, 'hltv', %s, 'match_result', 'normal', 'pending', %s)
                        ON CONFLICT DO NOTHING
                    """, (
                        headline,
                        content,
                        match['url'],
                        Json({
                            'hltv_match_id': match['match_id'],
                            'team1': match['team1'],
                            'team2': match['team2'],
                            'score1': match['score1'],
                            'score2': match['score2'],
                            'winner': match['winner'],
                            'event': match['event'],
                            'community_vibe': vibe,
                            'match_context': match.get('match_context', {}),
                        })
                    ))
                
                self.db_conn.commit()
                logger.info(f"💾 Stored {len(matches)} match results")
                
        except Exception as e:
            logger.error(f"❌ Database insert failed: {e}")
            self.db_conn.rollback()
    
    @staticmethod
    def _clean_result_event_name(context: str) -> str:
        event_name = re.sub(r'\s+', ' ', str(context or '')).strip(' .')
        event_name = re.sub(
            r'^(?:win|wins|claim|claims|take|takes|secure|secures|lift|lifts)\s+(?:the\s+)?',
            '',
            event_name,
            flags=re.IGNORECASE,
        )
        event_name = re.sub(
            r'^(?:qualify|qualifies|qualified)\s+for\s+(?:the\s+)?',
            '',
            event_name,
            flags=re.IGNORECASE,
        )
        return event_name.strip(' .')

    @classmethod
    def _parse_result_news_headline(cls, headline: str) -> Optional[Dict[str, str]]:
        """Extract structured result data from HLTV article headlines."""
        headline = re.sub(r'\s+', ' ', str(headline or '')).strip()
        if not headline:
            return None

        for pattern in cls._RESULT_NEWS_PATTERNS:
            match = pattern.search(headline)
            if not match:
                continue

            winner = match.group('winner').strip()
            loser = match.group('loser').strip()
            if not winner or not loser:
                continue

            return {
                'team1': winner,
                'team2': loser,
                'score1': match.group('score1'),
                'score2': match.group('score2'),
                'winner': winner,
                'event': cls._clean_result_event_name(match.group('context') or ''),
                'result_source': 'hltv_news_headline',
                'prefer_generated_media': 'premium_result',
            }

        return None

    @classmethod
    def _build_news_event_payload(cls, news: Dict[str, Any]) -> Dict[str, Any]:
        headline = str(news.get('headline') or '')
        metadata: Dict[str, Any] = {'hltv_article_id': news.get('article_id')}

        result_metadata = cls._parse_result_news_headline(headline)
        if result_metadata:
            metadata.update(result_metadata)
            return {'category': 'match_result', 'metadata': metadata}

        category = cls._classify_news_category(headline)
        return {'category': category, 'metadata': metadata}

    @staticmethod
    def _classify_news_category(headline: str) -> str:
        """Classify HLTV news article into the right category based on headline."""
        h = headline.lower()
        roster_signals = ['sign', 'leave', 'join', 'bench', 'replace', 'roster', 'transfer', 'acquire', 'release', 'step down', 'step up']
        if any(s in h for s in roster_signals):
            return 'roster_change'
        return 'cs2'

    def store_news(self, news_items: List[Dict[str, Any]]):
        """Store news articles in database"""
        if not news_items:
            return
        
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                for news in news_items:
                    payload = self._build_news_event_payload(news)
                    cur.execute("""
                        INSERT INTO twitter_bot.events
                        (headline, source, source_url, category, urgency, status, metadata)
                        VALUES (%s, 'hltv', %s, %s, %s, 'pending', %s)
                        ON CONFLICT DO NOTHING
                    """, (
                        news['headline'],
                        news['url'],
                        payload['category'],
                        news['urgency'],
                        Json(payload['metadata'])
                    ))
                
                self.db_conn.commit()
                logger.info(f"💾 Stored {len(news_items)} news articles")
                
        except Exception as e:
            logger.error(f"❌ Database insert failed: {e}")
            self.db_conn.rollback()
    
    async def run_cycle(self):
        """Run one monitoring cycle"""
        logger.info("🔄 Starting HLTV monitoring cycle...")
        
        # Scrape matches and news in parallel
        matches_task = self.scrape_matches()
        news_task = self.scrape_news()
        
        matches, news = await asyncio.gather(matches_task, news_task)
        
        # For each new match result, enrich with community comments + tournament context
        for match in matches:
            try:
                comments, match_context = await self.scrape_match_page(match['url'])
                match['community_vibe'] = self.extract_community_vibe(
                    comments, match['team1'], match['team2']
                )
                match['match_context'] = match_context
                if match['community_vibe']['memes']:
                    logger.info(f"🧠 Memes for {match['team1']} vs {match['team2']}: "
                                f"{match['community_vibe']['memes'][:3]}")
                if match_context.get('stage_type'):
                    logger.info(f"🏟️  Context for {match['team1']} vs {match['team2']}: "
                                f"stage={match_context.get('stage_type')}, "
                                f"format={match_context.get('format', '?')}")
            except Exception as e:
                logger.warning(f"⚠️  Match page extraction failed for match {match['match_id']}: {e}")
                match['community_vibe'] = {}
                match['match_context'] = {}
        
        # Store results
        self.store_matches(matches)
        self.store_news(news)
        
        logger.info("✅ Completed HLTV monitoring cycle")
    
    async def run_forever(self):
        """Main loop - runs every 180 seconds (3 minutes)"""
        self.connect_db()
        logger.info("🚀 HLTV Monitor started")
        
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"❌ Cycle failed: {e}")
            
            # Wait 3 minutes between cycles
            await asyncio.sleep(180)
    
    def cleanup(self):
        """Cleanup resources"""
        if self.session:
            self.session.close()
        if self.db_conn:
            self.db_conn.close()


async def main():
    monitor = HLTVMonitor()
    try:
        await monitor.run_forever()
    except KeyboardInterrupt:
        logger.info("⏹️  Shutting down HLTV Monitor...")
    finally:
        monitor.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
