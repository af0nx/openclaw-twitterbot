#!/usr/bin/env python3
"""
RSS Aggregator - Twitter Bot Pipeline V2
Polls 13 RSS feeds every 120s using Scrapling's anti-detection capabilities
"""

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import List, Dict, Any
import os

import feedparser
from curl_cffi.requests import Session as CurlSession
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
import json

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# RSS Feed Sources — CS2 only
RSS_FEEDS = [
    {
        'url': 'https://www.hltv.org/rss/news',
        'category': 'cs2',
        'source': 'hltv',
        'pure_cs2': True,  # No filtering needed
    },
    {
        'url': 'https://store.steampowered.com/feeds/news/app/730',
        'category': 'cs2_update',
        'source': 'valve_cs2',
        'pure_cs2': True,  # Official CS2 game updates, patch notes, new cases
    },
    {
        'url': 'https://www.esportsinsider.com/feed/',
        'category': 'cs2',
        'source': 'esports_insider',
        'pure_cs2': False,  # Mixed esports — needs CS2 filter
    },
    {
        'url': 'https://www.dust2.us/rss',
        'category': 'cs2',
        'source': 'dust2us',
        'pure_cs2': True,  # NA CS2 coverage
    },
    {
        'url': 'https://bo3.gg/rss',
        'category': 'cs2',
        'source': 'bo3gg',
        'pure_cs2': True,  # CS2 match and roster news
    },
    {
        'url': 'https://www.dexerto.com/feed/',
        'category': 'cs2',
        'source': 'dexerto',
        'pure_cs2': False,  # Mixed gaming — needs CS2 filter
    },
]


class RSSAggregator:
    """RSS feed aggregator with Scrapling anti-detection"""
    
    # Primary CS2 keywords — definitive indicators
    CS2_PRIMARY = {
        'cs2', 'counter-strike', 'csgo', 'cs:go', 'counterstrike',
        'hltv', 'faceit',
    }
    
    # Secondary keywords — only count if combined with esports/gaming context
    CS2_SECONDARY = {
        'navi', 'vitality', 'faze', 'g2', 'liquid', 'astralis', 'mouz',
        'heroic', 'fnatic', 'spirit', 'furia', 'ence', 'cloud9',
        'blast premier', 'esl pro', 'iem ', 'pgl ',
        'awp', 'nuke', 'mirage', 'inferno', 'ancient', 'anubis', 'dust2',
        'zywoo', 's1mple', 'niko', 'device', 'm0nesy', 'donk',
        'natus vincere', 'mousesports', 'team spirit', 'team vitality',
    }
    
    # Ambiguous short keywords that need word-boundary matching
    _SECONDARY_AMBIGUOUS = {
        'ence', 'g2', 'spirit', 'heroic', 'liquid', 'ancient',
        'nuke', 'niko', 'device', 'faze', 'vitality',
    }
    _SECONDARY_SAFE = CS2_SECONDARY - _SECONDARY_AMBIGUOUS
    _SECONDARY_AMBIGUOUS_RE = re.compile(
        r'\b(?:' + '|'.join(re.escape(kw) for kw in _SECONDARY_AMBIGUOUS) + r')\b',
        re.IGNORECASE
    )
    
    # For backward compat
    CS2_KEYWORDS = CS2_PRIMARY | CS2_SECONDARY
    
    def __init__(self):
        self.db_conn = None
        self.session = None
        self.seen_hashes = set()
        self._curl_session = CurlSession(impersonate="chrome")
    
    def _count_secondary_hits(self, text: str) -> int:
        """Count secondary keyword matches using word boundaries for ambiguous ones."""
        hits = sum(1 for kw in self._SECONDARY_SAFE if kw in text)
        hits += len(self._SECONDARY_AMBIGUOUS_RE.findall(text))
        return hits
    
    def is_cs2_article(self, headline: str, content: str) -> bool:
        """Check if article is CS2-related (filters mixed-esports feeds)"""
        text = (headline + ' ' + content).lower()
        # Reject articles clearly about other games
        non_cs2 = {'valorant', 'vct ', 'champions tour', 'overwatch', 'owcs',
                   'dota 2', 'dota2', 'league of legends', ' lol ', ' lck', ' lpl',
                   'call of duty', 'fortnite', 'apex legends', 'rocket league',
                   'rainbow six', 'r6 siege', 'tekken', 'street fighter',
                   'first stand 2026', 'worlds 2026', 'msi 2026',
                   'nintendo', 'switch 2', 'playstation', 'xbox', 'zelda',
                   'star fox', 'mario', 'pokemon', 'genshin', 'marvel',
                   'diablo', 'warzone', 'pubg', 'minecraft', 'roblox',
                   'impossible foods', 'lawsuit', 'copyright battle'}
        if any(kw in text for kw in non_cs2):
            return False
        # Primary keyword = definitely CS2
        if any(kw in text for kw in self.CS2_PRIMARY):
            return True
        # Secondary keywords need at least 2 matches (team + context)
        return self._count_secondary_hits(text) >= 2
        
    def connect_db(self):
        """Establish PostgreSQL connection"""
        try:
            self.db_conn = psycopg2.connect(os.getenv('DATABASE_URL'))
            logger.info("✅ Connected to PostgreSQL")
            
            # Load existing event hashes to avoid duplicates
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT MD5(headline || COALESCE(source_url, ''))
                    FROM twitter_bot.events
                    WHERE created_at > NOW() - INTERVAL '7 days'
                """)
                self.seen_hashes = {row[0] for row in cur.fetchall()}
                logger.info(f"Loaded {len(self.seen_hashes)} seen event hashes")
                
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise
    
    def generate_hash(self, headline: str, url: str) -> str:
        """Generate MD5 hash for deduplication"""
        content = headline + (url or '')
        return hashlib.md5(content.encode()).hexdigest()
    
    async def fetch_feed(self, feed_config: Dict[str, str]) -> List[Dict[str, Any]]:
        """Fetch and parse a single RSS feed"""
        try:
            timeout = int(os.getenv('SCRAPLING_TIMEOUT', 30))
            headers = {
                'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml, */*'
            }
            
            response = await asyncio.to_thread(
                self._curl_session.get,
                feed_config['url'],
                timeout=timeout,
                headers=headers,
                allow_redirects=True
            )
            
            if response.status_code != 200:
                logger.warning(f"⚠️  {feed_config['source']}: HTTP {response.status_code}")
                return []
            
            # Parse RSS
            feed = feedparser.parse(response.text)
            entries = []
            
            for entry in feed.entries[:10]:  # Limit to latest 10 per feed
                headline = entry.get('title', '').strip()
                url = entry.get('link', '')
                content = entry.get('summary', entry.get('description', ''))
                published = entry.get('published_parsed')
                
                # Generate dedup hash
                event_hash = self.generate_hash(headline, url)
                
                if event_hash in self.seen_hashes:
                    continue
                
                # Filter non-CS2 articles from mixed feeds (skip for pure CS2 sources)
                if not feed_config.get('pure_cs2') and not self.is_cs2_article(headline, content):
                    continue
                
                # Classify urgency based on keywords
                urgency = self.classify_urgency(headline, content)
                
                entries.append({
                    'headline': headline,
                    'content': content,
                    'source': feed_config['source'],
                    'source_url': url,
                    'category': feed_config['category'],
                    'urgency': urgency,
                    'hash': event_hash,
                    'published_at': datetime(*published[:6], tzinfo=timezone.utc) if published else None
                })
                
                self.seen_hashes.add(event_hash)
            
            if entries:
                logger.info(f"✅ {feed_config['source']}: {len(entries)} new entries")
            
            return entries
            
        except Exception as e:
            logger.error(f"❌ {feed_config['source']} fetch failed: {e}")
            return []
    
    def classify_urgency(self, headline: str, content: str) -> str:
        """Classify urgency based on keywords"""
        text = (headline + ' ' + content).lower()
        
        # Breaking news keywords
        breaking_keywords = [
            'breaking', 'just in', 'confirmed', 'announces', 
            'signs', 'leaves', 'joins', 'roster change'
        ]
        
        if any(kw in text for kw in breaking_keywords):
            return 'breaking'
        
        # Important keywords
        important_keywords = [
            'major', 'tournament', 'championship', 'regulation',
            'ban', 'fine', 'lawsuit', 'acquisition'
        ]
        
        if any(kw in text for kw in important_keywords):
            return 'important'
        
        return 'normal'
    
    def store_events(self, events: List[Dict[str, Any]]):
        """Store events in PostgreSQL"""
        if not events:
            return
        
        try:
            with self.db_conn.cursor() as cur:
                for event in events:
                    cur.execute("""
                        INSERT INTO twitter_bot.events 
                        (headline, content, source, source_url, category, urgency, status, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
                        ON CONFLICT DO NOTHING
                    """, (
                        event['headline'],
                        event['content'],
                        event['source'],
                        event['source_url'],
                        event['category'],
                        event['urgency'],
                        Json({'hash': event['hash'], 'published_at': event['published_at'].isoformat() if event['published_at'] else None})
                    ))
                
                self.db_conn.commit()
                logger.info(f"💾 Stored {len(events)} events to database")
                
        except Exception as e:
            logger.error(f"❌ Database insert failed: {e}")
            self.db_conn.rollback()
    
    async def run_cycle(self):
        """Run one aggregation cycle across all feeds"""
        logger.info("🔄 Starting RSS aggregation cycle...")
        
        tasks = [self.fetch_feed(feed) for feed in RSS_FEEDS]
        results = await asyncio.gather(*tasks)
        
        # Flatten results
        all_events = [event for result in results for event in result]
        
        if all_events:
            logger.info(f"📰 Total new events: {len(all_events)}")
            self.store_events(all_events)
        else:
            logger.info("ℹ️  No new events this cycle")
    
    async def run_forever(self):
        """Main loop - runs every 120 seconds"""
        self.connect_db()
        
        logger.info("🚀 RSS Aggregator started")
        
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"❌ Cycle failed: {e}")
            
            # Wait 120 seconds between cycles
            await asyncio.sleep(120)
    
    def cleanup(self):
        """Cleanup resources"""
        if self.db_conn:
            self.db_conn.close()


async def main():
    aggregator = RSSAggregator()
    try:
        await aggregator.run_forever()
    except KeyboardInterrupt:
        logger.info("⏹️  Shutting down RSS Aggregator...")
    finally:
        aggregator.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
