#!/usr/bin/env python3
"""
Thread Composer - Twitter Bot Pipeline V2
Composes daily threads (Pillar 10) using persona-conditioned generation
Formats as Twitter text threads with intelligent segmentation
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import os
import re

from dotenv import load_dotenv
import psycopg2

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


class ThreadComposer:
    """Daily thread generation and formatting"""
    
    def __init__(self):
        self.db_conn = None
        self.llm_client = OpenRouterClient()
        
    def connect_db(self):
        """Establish PostgreSQL connection"""
        try:
            self.db_conn = psycopg2.connect(os.getenv('DATABASE_URL'))
            logger.info("✅ Connected to PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise
    
    def gather_daily_context(self) -> Dict[str, Any]:
        """Gather events from the past 24 hours for thread context"""
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT category, headline, content, urgency
                    FROM twitter_bot.events
                    WHERE created_at > NOW() - INTERVAL '24 hours'
                    AND status = 'processed'
                    ORDER BY 
                        CASE urgency
                            WHEN 'breaking' THEN 1
                            WHEN 'important' THEN 2
                            ELSE 3
                        END,
                        created_at DESC
                    LIMIT 20
                """)
                
                events = []
                for row in cur.fetchall():
                    events.append({
                        'category': row[0],
                        'headline': row[1],
                        'content': row[2],
                        'urgency': row[3]
                    })
                
                logger.info(f"📊 Gathered {len(events)} events from past 24h")
                return {'events': events}
                
        except Exception as e:
            logger.error(f"❌ Failed to gather context: {e}")
            return {'events': []}
    
    def gather_top_performers(self) -> List[Dict[str, Any]]:
        """Get top 3 tweets from past 7 days"""
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT content, likes, replies, impressions
                    FROM twitter_bot.tweets_v2
                    WHERE posted_at > NOW() - INTERVAL '7 days'
                    AND status = 'posted'
                    ORDER BY (likes * 2 + replies * 3) DESC
                    LIMIT 3
                """)
                
                performers = []
                for row in cur.fetchall():
                    performers.append({
                        'content': row[0],
                        'likes': row[1],
                        'replies': row[2],
                        'impressions': row[3]
                    })
                
                return performers
                
        except Exception as e:
            logger.error(f"❌ Failed to fetch top performers: {e}")
            return []
    
    async def generate_thread_content(self, context: Dict[str, Any]) -> str:
        """Generate raw thread content using LLM"""
        system_prompt = """You are running a CS2 Twitter account. Write a daily recap thread about what happened in the last 24 hours.

LANGUAGE: B2 English. Simple words. Short sentences. Sound like a real fan, not a journalist.

Your voice:
- Casual and fun. Like talking to friends about CS2.
- Share what happened. Add your reactions.
- Keep it simple. No complex words.

Format:
- Start with a hook (e.g., "5 things that happened in CS2 today:")
- 8-12 points, each 1-2 simple sentences
- End with something funny or a hot take
- Write as continuous text. I will split it into tweets.

DO NOT:
- Use hashtags
- Make predictions
- Sound like a news reporter
- Use hard vocabulary

Context: You have 24 hours of CS2 news, match results, and community activity."""

        # Build context string
        events_text = "\n".join([
            f"[{e['category'].upper()}] {e['headline']}"
            for e in context['events'][:15]
        ])
        
        user_prompt = f"""Write a daily recap thread based on these events:

{events_text}

Generate the thread content as continuous prose. Start with a hook, develop 8-12 insights, end with a punchline. I'll handle splitting into tweets."""

        try:
            response = await self.llm_client.generate(
                prompt=user_prompt,
                system=system_prompt,
                tier='auto',
                max_tokens=800,
                temperature=0.8
            )
            
            logger.info("✅ Generated thread content")
            return response
            
        except Exception as e:
            logger.error(f"❌ Thread generation failed: {e}")
            raise
    
    def segment_into_tweets(self, content: str, max_length: int = 270) -> List[str]:
        """
        Intelligently segment thread content into tweet-sized chunks
        
        Args:
            content: Full thread text
            max_length: Max chars per tweet (leave room for thread numbering)
        
        Returns:
            List of tweet strings
        """
        # Split by sentences
        sentences = re.split(r'(?<=[.!?])\s+', content.strip())
        
        tweets = []
        current_tweet = ""
        
        for sentence in sentences:
            # If single sentence is too long, force split
            if len(sentence) > max_length:
                if current_tweet:
                    tweets.append(current_tweet.strip())
                    current_tweet = ""
                
                # Split long sentence at natural break points
                parts = re.split(r'[,;—]', sentence)
                for part in parts:
                    if len(current_tweet) + len(part) + 2 <= max_length:
                        current_tweet += part + " "
                    else:
                        if current_tweet:
                            tweets.append(current_tweet.strip())
                        current_tweet = part + " "
                continue
            
            # Try to add sentence to current tweet
            if len(current_tweet) + len(sentence) + 1 <= max_length:
                current_tweet += sentence + " "
            else:
                # Start new tweet
                if current_tweet:
                    tweets.append(current_tweet.strip())
                current_tweet = sentence + " "
        
        # Add final tweet
        if current_tweet:
            tweets.append(current_tweet.strip())
        
        logger.info(f"📝 Segmented into {len(tweets)} tweets")
        return tweets
    
    def add_thread_numbering(self, tweets: List[str]) -> List[str]:
        """Add tweet numbering (1/n format)"""
        n = len(tweets)
        
        numbered = []
        for i, tweet in enumerate(tweets, 1):
            if i == 1:
                # First tweet: no number
                numbered.append(tweet)
            else:
                # Subsequent tweets: add number at end
                numbering = f" ({i}/{n})"
                
                # Ensure we don't exceed 280 chars
                if len(tweet) + len(numbering) > 280:
                    tweet = tweet[:280 - len(numbering) - 3] + "..." + numbering
                else:
                    tweet += numbering
                
                numbered.append(tweet)
        
        return numbered
    
    async def compose_daily_thread(self) -> Optional[List[str]]:
        """
        Main composition pipeline
        
        Returns:
            List of tweet strings ready for posting
        """
        try:
            logger.info("🧵 Starting daily thread composition...")
            
            # Gather context
            context = self.gather_daily_context()
            
            if not context['events']:
                logger.warning("⚠️  No events to build thread from")
                return None
            
            # Generate content
            raw_content = await self.generate_thread_content(context)
            
            # Segment into tweets
            tweets = self.segment_into_tweets(raw_content)
            
            # Add numbering
            final_tweets = self.add_thread_numbering(tweets)
            
            # Validate
            for i, tweet in enumerate(final_tweets, 1):
                if len(tweet) > 280:
                    logger.error(f"❌ Tweet {i} exceeds 280 chars: {len(tweet)}")
                    raise ValueError(f"Tweet {i} too long")
            
            logger.info(f"✅ Composed thread: {len(final_tweets)} tweets")
            
            return final_tweets
            
        except Exception as e:
            logger.error(f"❌ Thread composition failed: {e}")
            return None
    
    async def store_thread(self, tweets: List[str]):
        """Store thread in database with proper ordering"""
        try:
            # Create parent event
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO twitter_bot.events
                    (headline, category, urgency, status, metadata)
                    VALUES ('Daily Thread', 'thread', 'normal', 'processed', %s)
                    RETURNING id
                """, ({"thread_date": datetime.now(timezone.utc).isoformat()},))
                
                event_id = cur.fetchone()[0]
                self.db_conn.commit()
            
            # Store each tweet
            tweet_ids = []
            for i, content in enumerate(tweets):
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO twitter_bot.tweets_v2
                        (event_id, content, pillar, status, metadata)
                        VALUES (%s, %s, 10, 'queued', %s)
                        RETURNING id
                    """, (
                        event_id,
                        content,
                        {"thread_position": i, "total_tweets": len(tweets)}
                    ))
                    
                    tweet_id = cur.fetchone()[0]
                    tweet_ids.append(tweet_id)
                    self.db_conn.commit()
            
            logger.info(f"✅ Stored thread: {len(tweet_ids)} tweets")
            return tweet_ids
            
        except Exception as e:
            logger.error(f"❌ Failed to store thread: {e}")
            raise
    
    async def run_daily_thread(self):
        """Main entry point - compose and store daily thread"""
        try:
            self.connect_db()
            
            # Check if thread already created today
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*)
                    FROM twitter_bot.events
                    WHERE category = 'thread'
                    AND DATE(created_at) = CURRENT_DATE
                """)
                
                count = cur.fetchone()[0]
                
                if count > 0:
                    logger.info("ℹ️  Daily thread already created today")
                    return
            
            # Compose thread
            tweets = await self.compose_daily_thread()
            
            if not tweets:
                logger.warning("⚠️  Thread composition failed")
                return
            
            # Store thread
            await self.store_thread(tweets)
            
            logger.info("🎉 Daily thread ready for posting")
            
        except Exception as e:
            logger.error(f"❌ Daily thread generation failed: {e}")
        finally:
            if self.db_conn:
                self.db_conn.close()


async def main():
    """CLI entry point"""
    composer = ThreadComposer()
    await composer.run_daily_thread()


if __name__ == '__main__':
    asyncio.run(main())
