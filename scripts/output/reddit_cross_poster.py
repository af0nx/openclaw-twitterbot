#!/usr/bin/env python3
"""
Reddit Cross-Poster - Twitter Bot Pipeline V2
Uses Marketing-for-Founders logic to convert Twitter threads into native Reddit posts
Strips CTAs, reformats for r/esports and r/sportsbook culture
Implements entangled cross-pollination (mirrors viral Twitter sub-debates)
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
import os
import re

from dotenv import load_dotenv
import psycopg2
import praw

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.openrouter_client import OpenRouterClient

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class RedditCrossPoster:
    """Transform Twitter threads into Reddit-native alpha drops"""
    
    def __init__(self):
        self.db_conn = None
        self.llm_client = OpenRouterClient()
        self.reddit = None
        
        # Forbidden terms for edge methodology protection
        self.signal_patterns = [
            r'\b(kelly|edge|clv|closing line value|sharp|steam|reverse line)\b',
            r'\b(probability model|expected value|ev\+|overlay)\b',
            r'\b(market inefficiency|mispriced|arbitrage)\b',
            r'\b(\d+\.\d+% edge|\d+\.\d+x kelly)\b'
        ]
        
    def connect_db(self):
        """Establish PostgreSQL connection"""
        try:
            self.db_conn = psycopg2.connect(os.getenv('DATABASE_URL'))
            logger.info("✅ Connected to PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise
    
    def init_reddit_client(self):
        """Initialize PRAW Reddit client"""
        try:
            self.reddit = praw.Reddit(
                client_id=os.getenv('REDDIT_CLIENT_ID'),
                client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
                user_agent=os.getenv('REDDIT_USER_AGENT', 'TwitterBot/1.0'),
                username=os.getenv('REDDIT_USERNAME'),
                password=os.getenv('REDDIT_PASSWORD')
            )
            
            logger.info(f"✅ Reddit client initialized as u/{self.reddit.user.me()}")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Reddit client: {e}")
            raise
    
    def fetch_top_thread(self) -> Optional[Dict[str, Any]]:
        """Fetch the top-performing thread from past 7 days"""
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        t.event_id,
                        e.headline,
                        ARRAY_AGG(t.content ORDER BY (t.metadata->>'thread_position')::int) as tweets,
                        SUM(t.likes) as total_likes,
                        SUM(t.replies) as total_replies,
                        SUM(t.impressions) as total_impressions
                    FROM twitter_bot.tweets_v2 t
                    JOIN twitter_bot.events e ON t.event_id = e.id
                    WHERE t.pillar = 10  -- Daily threads
                    AND t.posted_at > NOW() - INTERVAL '7 days'
                    AND t.status = 'posted'
                    GROUP BY t.event_id, e.headline
                    ORDER BY (SUM(t.likes) * 2 + SUM(t.replies) * 3) DESC
                    LIMIT 1
                """)
                
                row = cur.fetchone()
                if not row:
                    logger.warning("⚠️  No threads from past 7 days")
                    return None
                
                return {
                    'event_id': str(row[0]),
                    'headline': row[1],
                    'tweets': row[2],
                    'total_likes': row[3],
                    'total_replies': row[4],
                    'total_impressions': row[5]
                }
                
        except Exception as e:
            logger.error(f"❌ Failed to fetch top thread: {e}")
            return None
    
    def check_entanglement_trigger(self, event_id: str) -> Optional[Dict[str, Any]]:
        """
        Check if thread has viral sub-debate (entanglement phase shift)
        
        Trigger: Any reply sub-thread with ≥15 replies OR ≥50 likes
        within 2 hours of thread posting
        
        Returns:
            Dict with top sub-debate details if triggered, None otherwise
        """
        try:
            # In production, this would query Twitter API for reply tree
            # For now, simulate with database metadata
            
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT metadata
                    FROM twitter_bot.events
                    WHERE id = %s
                """, (event_id,))
                
                row = cur.fetchone()
                if not row or not row[0]:
                    return None
                
                metadata = row[0]
                
                # Check for viral_sub_debate marker
                if 'viral_sub_debate' in metadata:
                    logger.info("🔗 Entanglement trigger detected")
                    return metadata['viral_sub_debate']
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Failed to check entanglement: {e}")
            return None
    
    def strip_edge_signals(self, content: str) -> str:
        """
        Remove specific odds values, bookie names, and methodology hints
        that could reveal our edge to competitor analysts
        """
        cleaned = content
        
        # Remove specific odds
        cleaned = re.sub(r'-\d{3,}', '[odds]', cleaned)  # -180, -240
        cleaned = re.sub(r'\+\d{3,}', '[odds]', cleaned)  # +150
        
        # Remove bookie names
        bookies = ['DraftKings', 'FanDuel', 'BetMGM', 'Caesars', 'Bet365']
        for bookie in bookies:
            cleaned = re.sub(bookie, '[sportsbook]', cleaned, flags=re.IGNORECASE)
        
        # Remove edge methodology terms
        for pattern in self.signal_patterns:
            cleaned = re.sub(pattern, '[analysis]', cleaned, flags=re.IGNORECASE)
        
        return cleaned
    
    async def transform_for_reddit(self, thread: Dict[str, Any], sub_debate: Optional[Dict] = None) -> Dict[str, str]:
        """
        Transform Twitter thread into Reddit-native format
        Uses Marketing-for-Founders "Value-First Architecture"
        """
        system_prompt = """You are turning a Twitter thread into a Reddit post.

LANGUAGE: B2 English. Clear and simple. Longer than tweets but still easy to read.

Reddit style:
- More detail than Twitter, but still simple language
- Bullet points. Bold key words. TL;DR section.
- No emojis. More facts and analysis.
- No self-promotion. Just share useful info.

Your job: Make the Twitter thread into a Reddit post that feels like a helpful community member sharing what they know."""

        # Concatenate thread tweets
        thread_text = "\n\n".join(thread['tweets'])
        
        # Build prompt
        user_prompt = f"""Transform this Twitter thread into a Reddit post for r/esports:

ORIGINAL THREAD:
{thread_text}
"""
        
        if sub_debate:
            user_prompt += f"""

VIRAL SUB-DEBATE (add as "Community Hot Take" section):
{sub_debate.get('summary', 'No summary available')}
"""
        
        user_prompt += """

FORMAT AS:
- Compelling title (no clickbait)
- TL;DR section
- Main body with bullet points and bold key terms
- Optional "Community Hot Take" section if viral sub-debate provided
- NO links, NO CTAs, NO self-promotion

Strip any reference to specific bookmakers or odds values (replace with generic terms)."""

        try:
            response = await self.llm_client.generate(
                prompt=user_prompt,
                system=system_prompt,
                tier='auto',
                max_tokens=1200,
                temperature=0.7
            )
            
            # Parse response into title and body
            lines = response.strip().split('\n')
            title = lines[0].replace('Title:', '').replace('#', '').strip()
            body = '\n'.join(lines[1:]).strip()
            
            # Strip edge signals
            body = self.strip_edge_signals(body)
            
            logger.info("✅ Transformed thread for Reddit")
            
            return {
                'title': title[:300],  # Reddit title limit
                'body': body[:40000]   # Reddit post limit
            }
            
        except Exception as e:
            logger.error(f"❌ Reddit transformation failed: {e}")
            raise
    
    def post_to_reddit(self, subreddit_name: str, title: str, body: str) -> Optional[str]:
        """
        Post to Reddit
        
        Returns:
            Post URL if successful
        """
        try:
            subreddit = self.reddit.subreddit(subreddit_name)
            
            submission = subreddit.submit(
                title=title,
                selftext=body,
                send_replies=True
            )
            
            post_url = f"https://reddit.com{submission.permalink}"
            logger.info(f"✅ Posted to r/{subreddit_name}: {post_url}")
            
            return post_url
            
        except Exception as e:
            logger.error(f"❌ Failed to post to r/{subreddit_name}: {e}")
            return None
    
    async def run_weekly_cross_post(self):
        """Main entry point - weekly Reddit cross-posting"""
        try:
            self.connect_db()
            self.init_reddit_client()
            
            # Check if already posted this week
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*)
                    FROM twitter_bot.reddit_posts
                    WHERE posted_at > NOW() - INTERVAL '7 days'
                """)
                
                count = cur.fetchone()[0]
                
                if count > 0:
                    logger.info("ℹ️  Reddit post already created this week")
                    return
            
            # Fetch top thread
            thread = self.fetch_top_thread()
            
            if not thread:
                logger.warning("⚠️  No thread to cross-post")
                return
            
            # Check for entanglement trigger
            sub_debate = self.check_entanglement_trigger(thread['event_id'])
            
            if sub_debate:
                logger.info("🔗 Applying entangled cross-pollination")
            
            # Transform to Reddit format
            reddit_content = await self.transform_for_reddit(thread, sub_debate)
            
            # Post to r/esports
            post_url = self.post_to_reddit(
                'esports',
                reddit_content['title'],
                reddit_content['body']
            )
            
            if post_url:
                # Log cross-post
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO twitter_bot.reddit_posts
                        (thread_event_id, subreddit, title, url, entangled, posted_at)
                        VALUES (%s, 'esports', %s, %s, %s, NOW())
                    """, (
                        thread['event_id'],
                        reddit_content['title'],
                        post_url,
                        sub_debate is not None
                    ))
                    self.db_conn.commit()
                
                logger.info("🎉 Weekly Reddit cross-post complete")
            
        except Exception as e:
            logger.error(f"❌ Reddit cross-posting failed: {e}")
        finally:
            if self.db_conn:
                self.db_conn.close()


async def main():
    """CLI entry point"""
    poster = RedditCrossPoster()
    await poster.run_weekly_cross_post()


if __name__ == '__main__':
    asyncio.run(main())
