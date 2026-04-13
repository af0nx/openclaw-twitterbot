#!/usr/bin/env python3
"""
VIP HITL Telegram Bot - Twitter Bot Pipeline V2
Human-In-The-Loop approval gateway for VIP replies
Presents generated replies via Telegram with Approve/Reject/Regenerate buttons
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
import os

from dotenv import load_dotenv
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.content_generator import ContentGenerator
from utils.account_quota import free_account_slot
from utils.db_utils import ensure_db_connection
from utils.runtime_schema import ensure_runtime_schema_extensions

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class HITLTelegram:
    """Telegram bot for HITL approval of VIP replies"""
    
    def __init__(self):
        self.db_conn = None
        self.generator = ContentGenerator()
        self._app = None  # Set by run() after Application is built
        self._schema_ready = False
        
        # Access control
        self.allowed_user_ids = self._parse_allowed_users()
        
        # Telegram bot token
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN not set")
        
        # HITL timeout (hours before auto-action on un-reviewed tweets)
        self.hitl_timeout_hours = int(os.getenv('HITL_TIMEOUT_HOURS', 8))
        
    def _parse_allowed_users(self) -> set:
        """Parse allowed Telegram user IDs from env"""
        users_str = os.getenv('TELEGRAM_ALLOWED_USER_IDS', '')
        if not users_str:
            logger.warning("⚠️  No TELEGRAM_ALLOWED_USER_IDS set - bot will reject all interactions")
            return set()
        
        return set(int(uid.strip()) for uid in users_str.split(',') if uid.strip())
    
    def connect_db(self):
        """Establish PostgreSQL connection with auto-reconnect"""
        try:
            self.db_conn = ensure_db_connection(self.db_conn)
            if not self._schema_ready:
                ensure_runtime_schema_extensions(self.db_conn)
                self._schema_ready = True
            self.generator.connect_db()
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise

    def _ensure_db(self):
        """Lightweight reconnect guard"""
        self.db_conn = ensure_db_connection(self.db_conn)
        if not self._schema_ready:
            ensure_runtime_schema_extensions(self.db_conn)
            self._schema_ready = True
    
    def check_authorization(self, user_id: int) -> bool:
        """Check if user is authorized"""
        authorized = user_id in self.allowed_user_ids
        if not authorized:
            logger.warning(f"🚫 Unauthorized access attempt from user {user_id}")
        return authorized
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        
        if not self.check_authorization(user_id):
            await update.message.reply_text("🚫 Unauthorized access")
            return
        
        await update.message.reply_text(
            "🤖 Twitter Bot HITL Gateway\n\n"
            "I'll notify you when VIP engagement opportunities are ready for review.\n\n"
            "Commands:\n"
            "/pending - Show pending approvals\n"
            "/stats - Show today's stats"
        )
    
    async def pending_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show pending HITL tweets"""
        user_id = update.effective_user.id
        
        if not self.check_authorization(user_id):
            await update.message.reply_text("🚫 Unauthorized")
            return
        
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*)
                    FROM twitter_bot.tweets_v2
                    WHERE status = 'hitl_pending'
                    AND hitl_requested_at > NOW() - INTERVAL '%s hours'
                """, (self.hitl_timeout_hours,))
                
                count = cur.fetchone()[0]
            
            await update.message.reply_text(f"📋 Pending approvals: {count}")
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch pending: {e}")
            await update.message.reply_text("❌ Error fetching pending tweets")
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show today's stats"""
        user_id = update.effective_user.id
        
        if not self.check_authorization(user_id):
            await update.message.reply_text("🚫 Unauthorized")
            return
        
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT writes_executed, writes_reserved
                    FROM twitter_bot.api_quotas
                    WHERE date = CURRENT_DATE
                """)
                
                row = cur.fetchone()
                if not row:
                    await update.message.reply_text("ℹ️  No stats for today yet")
                    return
                
                executed, reserved = row
                total = executed + reserved
                daily_cap = int(os.getenv('DAILY_TWEET_CAP', 100))
                remaining = daily_cap - total
            
            stats_text = (
                f"📊 <b>Today's Stats</b>\n\n"
                f"✅ Posted: {executed}\n"
                f"⏳ Reserved: {reserved}\n"
                f"📈 Total committed: {total}/{daily_cap}\n"
                f"🎯 Slots available: {remaining}"
            )
            
            await update.message.reply_text(stats_text, parse_mode='HTML')
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch stats: {e}")
            await update.message.reply_text("❌ Error fetching stats")
    
    async def send_hitl_request(self, tweet_id: str):
        """Send HITL approval request to Telegram"""
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT t.id, t.content, t.reply_target_id, t.pillar,
                           e.headline, e.metadata, e.content, e.source,
                           t.media_path, t.is_thread, t.thread_tweets, t.quote_tweet_id
                    FROM twitter_bot.tweets_v2 t
                    LEFT JOIN twitter_bot.events e ON t.event_id = e.id
                    WHERE t.id = %s AND t.status = 'hitl_pending'
                """, (tweet_id,))
                
                row = cur.fetchone()
                if not row:
                    logger.warning(f"⚠️  Tweet {tweet_id} not found")
                    return
                
                tweet_data = {
                    'id': str(row[0]),
                    'content': row[1],
                    'reply_target_id': row[2],
                    'pillar': row[3],
                    'headline': row[4],
                    'metadata': row[5] or {},
                    'event_content': row[6] or '',
                    'source': row[7] or '',
                    'media_path': row[8],
                    'is_thread': row[9] or False,
                    'thread_tweets': row[10],
                    'quote_tweet_id': row[11]
                }
            
            # Extract VIP info from metadata
            vip_username = tweet_data['metadata'].get('vip_username', '')
            
            # Different format for VIP replies (pillar 12) vs original tweets (pillars 1-11)
            is_vip_reply = tweet_data['pillar'] == 12 and vip_username
            
            # HTML escape for Telegram HTML parse mode (much more reliable than Markdown)
            import html as _html
            def _esc(text: str) -> str:
                return _html.escape(str(text))
            
            if is_vip_reply:
                vip_tweet_text = tweet_data['event_content'][:300] if tweet_data['event_content'] else '(no content captured)'
                header = "VIP REPLY"
                context_block = (
                    f"<b>Target:</b> @{_esc(vip_username)}\n"
                    f"<b>Their tweet:</b> {_esc(vip_tweet_text)}\n"
                )
            else:
                source = tweet_data.get('source', '') or 'unknown'
                source_label = {
                    'hltv': 'HLTV', 'esports_insider': 'Esports Insider',
                    'valve_cs2': 'Valve CS2 Update',
                    'twitter': 'Twitter'
                }.get(source, source.upper())
                headline_text = tweet_data.get('headline', 'No headline')[:200]
                header = f"PILLAR {tweet_data['pillar']}"
                context_block = (
                    f"<b>Source:</b> {_esc(source_label)}\n"
                    f"<b>Event:</b> {_esc(headline_text)}\n"
                )
            
            message = (
                f"🚨 <b>{_esc(header)} — HITL REVIEW</b> 🚨\n\n"
                f"{context_block}\n"
                f"<b>Proposed {'Reply' if is_vip_reply else 'Tweet'}:</b>\n"
                f"<i>{_esc(tweet_data['content'])}</i>\n\n"
                f"<b>Chars:</b> {len(tweet_data['content'])}/280\n"
                f"<b>Tweet ID:</b> <code>{tweet_id[:8]}...</code>"
            )
            
            # Additional info badges
            badges = []
            if tweet_data.get('media_path'):
                badges.append("📸 Media attached")
            if tweet_data.get('is_thread'):
                badges.append(f"🧵 Thread ({len(tweet_data.get('thread_tweets', []))} tweets)")
            if tweet_data.get('quote_tweet_id') and not is_vip_reply:
                badges.append("💬 Quote Tweet")
            if badges:
                message += "\n" + " | ".join(badges)
            
            # Build inline keyboard
            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve:{tweet_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject:{tweet_id}")
                ],
                [
                    InlineKeyboardButton("🔄 Regenerate", callback_data=f"regen:{tweet_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send to all allowed users via the running bot instance
            bot = self._app.bot if self._app else Application.builder().token(self.bot_token).build().bot
            for user_id in self.allowed_user_ids:
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=message,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                    logger.info(f"📱 Sent HITL request to user {user_id}")
                except Exception as e:
                    logger.error(f"❌ Failed to send to user {user_id}: {e}")
            
        except Exception as e:
            logger.error(f"❌ Failed to send HITL request: {e}")
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        user_id = query.from_user.id
        
        # Authorization check
        if not self.check_authorization(user_id):
            await query.answer("🚫 Unauthorized", show_alert=True)
            return
        
        await query.answer()
        
        # Parse callback data
        action, tweet_id = query.data.split(':', 1)
        
        try:
            if action == 'approve':
                await self.approve_tweet(tweet_id, user_id, query)
            elif action == 'reject':
                await self.reject_tweet(tweet_id, user_id, query)
            elif action == 'regen':
                await self.regenerate_tweet(tweet_id, user_id, query)
            else:
                await query.edit_message_text("❌ Unknown action")
                
        except Exception as e:
            logger.error(f"❌ Callback failed: {e}")
            await query.edit_message_text(f"❌ Error: {str(e)}")
    
    async def approve_tweet(self, tweet_id: str, user_id: int, query):
        """Approve a tweet for posting — clears scheduled time so it posts immediately"""
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET status = 'queued',
                        hitl_approved_by = %s,
                        hitl_approved_at = NOW(),
                        scheduled_post_at = NULL,
                        updated_at = NOW()
                    WHERE id = %s AND status IN ('hitl_pending', 'queued')
                    RETURNING content
                """, (str(user_id), tweet_id))
                
                row = cur.fetchone()
                if not row:
                    await query.edit_message_text("⚠️  Tweet not found or already processed")
                    return
                
                content = row[0]
                self.db_conn.commit()
            
            logger.info(f"✅ Tweet {tweet_id} approved by user {user_id}")
            
            import html as _html
            await query.edit_message_text(
                f"✅ <b>APPROVED</b>\n\n"
                f"Tweet queued for posting:\n"
                f"<i>{_html.escape(content)}</i>\n\n"
                f"Approved by user {user_id}",
                parse_mode='HTML'
            )
            
        except Exception as e:
            logger.error(f"❌ Failed to approve: {e}")
            raise
    
    async def reject_tweet(self, tweet_id: str, user_id: int, query):
        """Reject a tweet"""
        try:
            self._ensure_db()
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET status = 'rejected',
                        updated_at = NOW()
                    WHERE id = %s AND status = 'hitl_pending'
                    RETURNING content, COALESCE(account_bucket, 'main')
                """, (tweet_id,))
                
                row = cur.fetchone()
                if not row:
                    await query.edit_message_text("⚠️  Tweet not found")
                    return
                
                content = row[0]
                account_bucket = row[1] or 'main'
                self.db_conn.commit()
            
            # Free the reserved slot
            free_account_slot(self.db_conn, account_bucket)
            
            logger.info(f"❌ Tweet {tweet_id} rejected by user {user_id}")
            
            import html as _html
            await query.edit_message_text(
                f"❌ <b>REJECTED</b>\n\n"
                f"Tweet discarded:\n"
                f"<i>{_html.escape(content)}</i>\n\n"
                f"Slot freed.",
                parse_mode='HTML'
            )
            
        except Exception as e:
            logger.error(f"❌ Failed to reject: {e}")
            raise
    
    async def regenerate_tweet(self, tweet_id: str, user_id: int, query):
        """Regenerate a tweet with different parameters"""
        try:
            self._ensure_db()
            await query.edit_message_text("🔄 Regenerating tweet...")
            
            # Fetch original event
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT e.id, e.headline, e.content, e.category, e.metadata
                    FROM twitter_bot.tweets_v2 t
                    JOIN twitter_bot.events e ON t.event_id = e.id
                    WHERE t.id = %s
                """, (tweet_id,))
                
                row = cur.fetchone()
                if not row:
                    await query.edit_message_text("⚠️  Event not found")
                    return
                
                event = {
                    'id': str(row[0]),
                    'headline': row[1],
                    'content': row[2],
                    'category': row[3],
                    'metadata': row[4]
                }
            
            # Regenerate with higher temperature
            result = self.generator.dual_agent_generate(
                event,
                pillar=12,
                temperature=0.9  # More creative
            )
            
            new_content = result['final_text']
            
            # Update tweet
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET content = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                """, (new_content, tweet_id))
                
                self.db_conn.commit()
            
            logger.info(f"🔄 Tweet {tweet_id} regenerated")
            
            # Send new HITL request
            await self.send_hitl_request(tweet_id)
            
            await query.edit_message_text(
                "🔄 **REGENERATED**\n\n"
                "New version sent for review."
            )
            
        except Exception as e:
            logger.error(f"❌ Failed to regenerate: {e}")
            await query.edit_message_text(f"❌ Regeneration failed: {str(e)}")
    
    async def check_pending_hitl(self):
        """Check for new HITL-pending tweets and send notifications"""
        try:
            with self.db_conn.cursor() as cur:
                cur.execute("""
                    SELECT id
                    FROM twitter_bot.tweets_v2
                    WHERE status = 'hitl_pending'
                    AND hitl_requested_at IS NULL
                    ORDER BY created_at ASC
                    LIMIT 5
                """)
                
                pending_ids = [str(row[0]) for row in cur.fetchall()]
            
            if not pending_ids:
                return
            
            logger.info(f"📱 Sending {len(pending_ids)} HITL requests")
            
            for tweet_id in pending_ids:
                # Mark as requested
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        UPDATE twitter_bot.tweets_v2
                        SET hitl_requested_at = NOW()
                        WHERE id = %s
                    """, (tweet_id,))
                    self.db_conn.commit()
                
                # Send notification
                await self.send_hitl_request(tweet_id)
                await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"❌ Failed to check pending HITL: {e}")
    
    async def expire_old_hitl(self):
        """Handle timed-out HITL requests: auto-promote safe ones, expire risky ones.

        Tweets that passed both fact-check and tone validation were only in HITL
        as an extra caution layer.  If nobody reviews them within the timeout
        window, auto-promote them to 'queued' so the poster picks them up.

        Pillar 16 (disagreement replies to real people) and tweets older than
        24 hours are still expired — they're either too risky or too stale.
        """
        try:
            with self.db_conn.cursor() as cur:
                timeout = self.hitl_timeout_hours

                # --- 1. Auto-promote safe tweets to the posting queue ---
                # Quality-passed, not disagreement replies, and fresh enough (<24h)
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET status = 'queued',
                        updated_at = NOW(),
                        auto_approved = TRUE
                    WHERE status = 'hitl_pending'
                    AND hitl_requested_at < NOW() - INTERVAL '%s hours'
                    AND fact_checked = TRUE
                    AND tone_validated = TRUE
                    AND pillar != 16
                    AND created_at > NOW() - INTERVAL '24 hours'
                    RETURNING id, COALESCE(account_bucket, 'main')
                """, (timeout,))

                promoted_rows = cur.fetchall()
                self.db_conn.commit()

                if promoted_rows:
                    logger.info(
                        f"⏰ Auto-promoted {len(promoted_rows)} safe HITL tweets "
                        f"to posting queue (un-reviewed >{timeout}h)"
                    )

                # --- 2. Expire risky or stale tweets ---
                # Pillar 16 (disagreement), quality failures, or >24h old
                cur.execute("""
                    UPDATE twitter_bot.tweets_v2
                    SET status = 'expired',
                        updated_at = NOW()
                    WHERE status = 'hitl_pending'
                    AND hitl_requested_at < NOW() - INTERVAL '%s hours'
                    RETURNING id, COALESCE(account_bucket, 'main')
                """, (timeout,))

                expired_rows = [(str(row[0]), row[1] or 'main') for row in cur.fetchall()]

                self.db_conn.commit()

                for _, bucket in expired_rows:
                    free_account_slot(self.db_conn, bucket)

                if expired_rows:
                    logger.info(f"⏰ Expired {len(expired_rows)} risky/stale HITL requests")
            
        except Exception as e:
            logger.error(f"❌ Failed to expire old HITL: {e}")
    
    async def background_tasks(self):
        """Run background maintenance tasks"""
        while True:
            try:
                self._ensure_db()
                # Rollback any stale implicit transaction so CURRENT_DATE/NOW() are fresh
                try:
                    self.db_conn.rollback()
                except Exception:
                    pass
                await self.check_pending_hitl()
                await self.expire_old_hitl()
            except Exception as e:
                logger.error(f"❌ Background task failed: {e}")
            
            await asyncio.sleep(60)  # Run every minute
    
    def run(self):
        """Start the Telegram bot"""
        self.connect_db()
        
        # Build application
        app = Application.builder().token(self.bot_token).build()
        
        # Add handlers
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(CommandHandler("pending", self.pending_command))
        app.add_handler(CommandHandler("stats", self.stats_command))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        
        # Schedule background tasks to start after bot initializes (inside running event loop)
        async def post_init(application):
            self._app = application
            asyncio.create_task(self.background_tasks())
        
        app.post_init = post_init
        
        logger.info("🚀 HITL Telegram Bot started")
        
        # Run bot
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    bot = HITLTelegram()
    bot.run()
