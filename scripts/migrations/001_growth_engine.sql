-- Twitter Bot Pipeline V2 — Growth Engine Migration
-- New tables: style_bank, media_library
-- New columns: tweets_v2.pruned, tweets_v2.pruned_at, tweets_v2.prune_attempted_at
-- New pillars: 13 (poll), 14 (conversation), 15 (style_take)
-- Run: psql $DATABASE_URL -f scripts/migrations/001_growth_engine.sql

SET search_path TO twitter_bot, public;

-- ============================================================
-- STYLE BANK — Viral tweet examples scraped from top CS2 accounts
-- Fed into Writer agent as few-shot examples
-- ============================================================
CREATE TABLE IF NOT EXISTS style_bank (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tweet_id TEXT UNIQUE NOT NULL,          -- Twitter tweet ID (dedup key)
    source_account TEXT NOT NULL,            -- @username
    source_style TEXT,                       -- style description
    text TEXT NOT NULL,                      -- cleaned tweet text (no t.co links)
    full_text TEXT,                          -- original full text
    likes INT DEFAULT 0,
    retweets INT DEFAULT 0,
    replies INT DEFAULT 0,
    quotes INT DEFAULT 0,
    bookmarks INT DEFAULT 0,
    views INT DEFAULT 0,
    engagement_score FLOAT DEFAULT 0,       -- weighted: likes + RT*2 + quotes*3
    has_image BOOLEAN DEFAULT FALSE,
    has_video BOOLEAN DEFAULT FALSE,
    media_urls JSONB,                        -- image URLs from tweet
    video_urls JSONB,                        -- video URLs from tweet
    char_count INT,
    scraped_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_style_bank_account ON style_bank(source_account);
CREATE INDEX IF NOT EXISTS idx_style_bank_score ON style_bank(engagement_score DESC);
CREATE INDEX IF NOT EXISTS idx_style_bank_scraped ON style_bank(scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_style_bank_media ON style_bank(has_image, has_video);

-- ============================================================
-- MEDIA LIBRARY — Discovered clips, images, memes
-- Used by engagement engine + tweet scheduler for media-rich posts
-- ============================================================
CREATE TABLE IF NOT EXISTS media_library (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL,                    -- 'hltv_gallery', 'reddit_GlobalOffensive', etc.
    source_url TEXT UNIQUE NOT NULL,         -- dedup key
    title TEXT,
    media_type TEXT NOT NULL,                -- 'image', 'video', 'twitch_clip', 'youtube_short'
    local_path TEXT,                         -- path to downloaded file
    engagement_score FLOAT DEFAULT 0,        -- from source (reddit upvotes, etc.)
    used_count INT DEFAULT 0,                -- how many times we've used this
    last_used_at TIMESTAMPTZ,
    metadata JSONB,                          -- flexible source-specific data
    discovered_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_media_library_source ON media_library(source);
CREATE INDEX IF NOT EXISTS idx_media_library_type ON media_library(media_type);
CREATE INDEX IF NOT EXISTS idx_media_library_score ON media_library(engagement_score DESC);
CREATE INDEX IF NOT EXISTS idx_media_library_used ON media_library(used_count);
CREATE INDEX IF NOT EXISTS idx_media_library_discovered ON media_library(discovered_at DESC);

-- ============================================================
-- NEW COLUMNS ON tweets_v2 — Pruning support
-- ============================================================
ALTER TABLE tweets_v2 ADD COLUMN IF NOT EXISTS pruned BOOLEAN DEFAULT FALSE;
ALTER TABLE tweets_v2 ADD COLUMN IF NOT EXISTS pruned_at TIMESTAMPTZ;
ALTER TABLE tweets_v2 ADD COLUMN IF NOT EXISTS prune_attempted_at TIMESTAMPTZ;
ALTER TABLE tweets_v2 ADD COLUMN IF NOT EXISTS auto_approved BOOLEAN DEFAULT FALSE;
ALTER TABLE tweets_v2 ADD COLUMN IF NOT EXISTS scheduled_post_at TIMESTAMPTZ;
ALTER TABLE tweets_v2 ADD COLUMN IF NOT EXISTS is_thread BOOLEAN DEFAULT FALSE;
ALTER TABLE tweets_v2 ADD COLUMN IF NOT EXISTS thread_tweets JSONB;

CREATE INDEX IF NOT EXISTS idx_tweets_pruned ON tweets_v2(pruned) WHERE pruned = FALSE;
CREATE INDEX IF NOT EXISTS idx_tweets_scheduled ON tweets_v2(scheduled_post_at) WHERE scheduled_post_at IS NOT NULL;

-- ============================================================
-- CLEANUP: Auto-prune old style_bank entries (>30 days)
-- ============================================================
CREATE OR REPLACE FUNCTION cleanup_style_bank()
RETURNS void AS $$
BEGIN
    DELETE FROM style_bank WHERE scraped_at < NOW() - INTERVAL '30 days';
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- CLEANUP: Auto-prune old media_library entries (>14 days, unused)
-- ============================================================
CREATE OR REPLACE FUNCTION cleanup_media_library()
RETURNS void AS $$
BEGIN
    DELETE FROM media_library
    WHERE discovered_at < NOW() - INTERVAL '14 days'
    AND used_count = 0;
END;
$$ LANGUAGE plpgsql;

-- Verify
SELECT 'style_bank' AS table_name, COUNT(*) AS rows FROM style_bank
UNION ALL
SELECT 'media_library', COUNT(*) FROM media_library;
