-- Twitter Bot Pipeline V2 - PostgreSQL Schema
-- Database: Railway PostgreSQL 16 (shared with SkinBetAI)
-- Schema: twitter_bot (isolated from signal_map)

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS vector;  -- pgvector for semantic embeddings

-- Create dedicated schema
CREATE SCHEMA IF NOT EXISTS twitter_bot;
SET search_path TO twitter_bot, public;

-- ============================================================
-- SIFTLY MEDIA ANALYSIS (Vision + OCR)
-- Unified in PostgreSQL (no more SQLite split-brain)
-- ============================================================
CREATE TABLE siftly_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_url TEXT NOT NULL,
    vip_author TEXT,
    raw_text TEXT,
    ocr_extracted_text TEXT,           -- Vision analysis of attached charts/memes
    has_media BOOLEAN DEFAULT FALSE,
    media_urls JSONB,                   -- Array of image URLs
    semantic_tags JSONB,                -- [{"tag": "cs2_roster", "confidence": 0.98}]
    embedding VECTOR(768),              -- Sentence transformer embedding for semantic search
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_siftly_source ON siftly_events(source_url);
CREATE INDEX idx_siftly_author ON siftly_events(vip_author);
CREATE INDEX idx_siftly_tags ON siftly_events USING GIN(semantic_tags);
CREATE INDEX idx_siftly_created ON siftly_events(created_at DESC);

-- ============================================================
-- EVENT REPOSITORY (Multi-Source News Ingestion)
-- ============================================================
CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    siftly_event_id UUID REFERENCES siftly_events(id) ON DELETE SET NULL,
    headline TEXT NOT NULL,
    content TEXT,
    source TEXT NOT NULL,               -- 'rss', 'twitter', 'hltv', 'manual'
    source_url TEXT,
    category TEXT,                      -- 'roster_change', 'match_result', 'regulation', 'drama'
    urgency TEXT DEFAULT 'normal',      -- 'breaking', 'important', 'normal', 'skip'
    status TEXT DEFAULT 'pending',      -- 'pending', 'generating', 'scheduled', 'posted', 'rejected'
    metadata JSONB,                     -- Flexible storage for source-specific data
    created_at TIMESTAMPTZ DEFAULT now(),
    processed_at TIMESTAMPTZ
);
CREATE INDEX idx_events_status ON events(status);
CREATE INDEX idx_events_urgency ON events(urgency);
CREATE INDEX idx_events_category ON events(category);
CREATE INDEX idx_events_created ON events(created_at DESC);
CREATE INDEX idx_events_twitter_vip_source_url
ON events(source_url)
WHERE source = 'twitter' AND category = 'vip_engagement' AND source_url IS NOT NULL;

-- ============================================================
-- GENERATED TWEET QUEUE
-- ============================================================
CREATE TABLE tweets_v2 (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES events(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'draft',        -- 'draft', 'hitl_pending', 'mirofish_veto', 
                                        -- 'queued', 'posted', 'failed', 'expired'
    pillar INT NOT NULL,                -- Content pillar (1-13)
    pillar_name TEXT,
    content TEXT NOT NULL,
    reply_target_id TEXT,               -- Original tweet ID we are replying to
    quote_tweet_id TEXT,                -- Tweet ID for quote tweets
    media_path TEXT,                    -- Path to generated media if applicable
    account_bucket TEXT,                -- main / live / replies
    
    -- Guardrails & Review
    mirofish_ratio_risk FLOAT,          -- 0.0 to 1.0 risk score from 100-agent swarm
    tone_validated BOOLEAN DEFAULT FALSE,
    fact_checked BOOLEAN DEFAULT FALSE,
    hitl_approved_by TEXT,              -- Telegram User ID if Pillar 12
    hitl_requested_at TIMESTAMPTZ,
    hitl_approved_at TIMESTAMPTZ,
    
    -- API Tracking
    twitter_tweet_id TEXT,              -- Foreign key matched to X API response
    posted_at TIMESTAMPTZ,
    posting_error TEXT,
    
    -- RLHF Feedback Loop Analytics Data
    impressions INT DEFAULT 0,
    likes INT DEFAULT 0,
    replies INT DEFAULT 0,
    retweets INT DEFAULT 0,
    quotes INT DEFAULT 0,
    bookmarks INT DEFAULT 0,
    engagement_rate FLOAT,              -- Calculated metric
    rlhf_classified TEXT,               -- 'top_5', 'bottom_5', 'neutral'
    
    -- Metadata
    generation_model TEXT,
    generation_cost_usd NUMERIC(10, 6),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_tweets_status ON tweets_v2(status);
CREATE INDEX idx_tweets_pillar ON tweets_v2(pillar);
CREATE INDEX idx_tweets_posted ON tweets_v2(posted_at DESC);
CREATE INDEX idx_tweets_rlhf ON tweets_v2(rlhf_classified);
CREATE INDEX idx_tweets_twitter_id ON tweets_v2(twitter_tweet_id);
CREATE INDEX idx_tweets_account_bucket ON tweets_v2(account_bucket);

-- ============================================================
-- STRICT 40/DAY RATE LIMIT TRACKER WITH PRE-COMMIT RESERVATION
-- ============================================================
CREATE TABLE api_quotas (
    date DATE PRIMARY KEY DEFAULT CURRENT_DATE,
    writes_executed INT DEFAULT 0,      -- Actually posted to X API
    writes_reserved INT DEFAULT 0,      -- Pre-committed slots (HITL pending, queued)
    reads_executed INT DEFAULT 0,
    hard_capped BOOLEAN DEFAULT FALSE,
    last_post_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE account_quotas (
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    account_bucket TEXT NOT NULL,
    writes_executed INT DEFAULT 0,
    writes_reserved INT DEFAULT 0,
    hard_capped BOOLEAN DEFAULT FALSE,
    last_post_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (date, account_bucket)
);

-- Reservation enforcement function
CREATE OR REPLACE FUNCTION reserve_tweet_slot()
RETURNS BOOLEAN AS $$
DECLARE
    current_total INT;
BEGIN
    -- Lock the row to prevent concurrent reservation races (TOCTOU)
    SELECT (writes_executed + writes_reserved) INTO current_total
    FROM api_quotas
    WHERE date = CURRENT_DATE
    FOR UPDATE;
    
    IF current_total IS NULL THEN
        -- First tweet of the day, initialize
        INSERT INTO api_quotas (date, writes_reserved) VALUES (CURRENT_DATE, 1);
        RETURN TRUE;
    ELSIF current_total < 38 THEN
        -- Space available, reserve slot
        UPDATE api_quotas 
        SET writes_reserved = writes_reserved + 1,
            updated_at = now()
        WHERE date = CURRENT_DATE;
        RETURN TRUE;
    ELSE
        -- Cap reached
        RETURN FALSE;
    END IF;
END;
$$ LANGUAGE plpgsql
   SET search_path = twitter_bot, public;

-- ============================================================
-- MONITORED VIP ACCOUNTS
-- ============================================================
CREATE TABLE monitored_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    twitter_username TEXT NOT NULL UNIQUE,
    twitter_user_id TEXT,
    list_type TEXT NOT NULL,            -- 'operator_ceo', 'tier1_streamer', 'journalist'
    auto_hitl_engage BOOLEAN DEFAULT TRUE,
    engagement_priority INT DEFAULT 5,  -- 1-10 scale
    notes TEXT,
    added_at TIMESTAMPTZ DEFAULT now(),
    last_checked_at TIMESTAMPTZ,
    active BOOLEAN DEFAULT TRUE
);
CREATE INDEX idx_monitored_username ON monitored_accounts(twitter_username);
CREATE INDEX idx_monitored_type ON monitored_accounts(list_type);
CREATE INDEX idx_monitored_active ON monitored_accounts(active);

-- ============================================================
-- SHADOWBAN CANARY LOG (Health Monitoring)
-- ============================================================
CREATE TABLE canary_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    twitter_tweet_id TEXT NOT NULL,
    test_result TEXT NOT NULL,          -- 'visible', 'ghost_banned', 'search_ban', 'timeout'
    scrapling_verification_ip TEXT,     -- Must be different IP than poster IP
    verification_method TEXT,           -- 'scrapling', 'api', 'manual'
    response_time_ms INT,
    timestamp TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_canary_result ON canary_logs(test_result);
CREATE INDEX idx_canary_timestamp ON canary_logs(timestamp DESC);

-- ============================================================
-- HITL ENGAGEMENT TRACKING (Factor #33 Feedback Loop Guard)
-- Used by SkinBetAI Factor #33 to exclude bot's own interactions
-- Prevents circular self-pollution in sentiment analysis
-- ============================================================
CREATE TABLE hitl_engaged_7d (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    twitter_username TEXT NOT NULL,
    our_reply_tweet_id TEXT NOT NULL,
    engaged_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_hitl_engaged_user ON hitl_engaged_7d(twitter_username);
CREATE INDEX idx_hitl_engaged_at ON hitl_engaged_7d(engaged_at DESC);

-- Daily cleanup function (called by Airflow)
CREATE OR REPLACE FUNCTION cleanup_hitl_engaged()
RETURNS void AS $$
BEGIN
    DELETE FROM hitl_engaged_7d
    WHERE engaged_at < now() - INTERVAL '7 days';
END;
$$ LANGUAGE plpgsql
   SET search_path = twitter_bot, public;

-- ============================================================
-- RLHF TUNING HISTORY (Auto-Learning System)
-- ============================================================
CREATE TABLE rlhf_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    week_start DATE NOT NULL,
    top_tweets UUID[],                  -- Array of tweet IDs
    bottom_tweets UUID[],               -- Array of tweet IDs
    analysis_summary TEXT,
    old_appendix TEXT,
    new_appendix TEXT,
    similarity_score FLOAT,             -- Cosine similarity vs original
    cumulative_similarity FLOAT,        -- vs Week 0 baseline
    applied BOOLEAN DEFAULT FALSE,
    rejection_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_rlhf_week ON rlhf_history(week_start DESC);

-- ============================================================
-- REDDIT CROSS-POST TRACKING
-- ============================================================
CREATE TABLE reddit_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_event_id UUID REFERENCES events(id),
    source_tweet_id UUID REFERENCES tweets_v2(id),
    subreddit TEXT NOT NULL,
    reddit_post_id TEXT,
    title TEXT NOT NULL,
    url TEXT,
    entangled BOOLEAN DEFAULT FALSE,    -- Was viral sub-debate incorporated?
    upvotes INT DEFAULT 0,
    comments INT DEFAULT 0,
    posted_at TIMESTAMPTZ DEFAULT now(),
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_reddit_subreddit ON reddit_posts(subreddit);
CREATE INDEX idx_reddit_posted ON reddit_posts(posted_at DESC);
CREATE INDEX idx_reddit_thread ON reddit_posts(thread_event_id);

-- ============================================================
-- SYSTEM ANALYTICS & METRICS
-- ============================================================
CREATE TABLE system_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    metric_name TEXT NOT NULL,
    metric_value NUMERIC,
    metric_metadata JSONB,
    timestamp TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_metrics_name ON system_metrics(metric_name);
CREATE INDEX idx_metrics_timestamp ON system_metrics(timestamp DESC);

-- ============================================================
-- GRANT PERMISSIONS (for separated DB users)
-- ============================================================
-- GRANT USAGE ON SCHEMA twitter_bot TO twitterbot_rw;
-- GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA twitter_bot TO twitterbot_rw;
-- GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA twitter_bot TO twitterbot_rw;
-- GRANT USAGE ON SCHEMA twitter_bot TO analytics_ro;
-- GRANT SELECT ON ALL TABLES IN SCHEMA twitter_bot TO analytics_ro;

-- ============================================================
-- SEED DATA (Initial VIP List)
-- ============================================================
INSERT INTO monitored_accounts (twitter_username, list_type, engagement_priority, notes) VALUES
    ('EsportsInsider', 'journalist', 9, 'Major esports news outlet'),
    ('hltv', 'journalist', 10, 'CS2 authority - auto-engage breaking news'),
    ('RichardLewisTV', 'journalist', 8, 'Industry commentator'),
    ('Slasher', 'journalist', 9, 'Esports reporter'),
    ('Thorin', 'journalist', 7, 'Analyst and historian'),
    ('StakeEddie', 'operator_ceo', 9, 'Stake.com co-founder'),
    ('TrainwrecksTV', 'tier1_streamer', 10, 'Kick co-founder, massive following'),
    ('xQc', 'tier1_streamer', 9, 'Top Kick streamer'),
    ('Asmongold', 'tier1_streamer', 8, 'Gaming/gambling content')
ON CONFLICT (twitter_username) DO NOTHING;

-- Initialize today's quota tracker
INSERT INTO api_quotas (date) VALUES (CURRENT_DATE)
ON CONFLICT (date) DO NOTHING;

-- ============================================================
-- HELPER VIEWS
-- ============================================================

-- View: Today's tweet performance
CREATE OR REPLACE VIEW todays_performance AS
SELECT 
    pillar,
    pillar_name,
    COUNT(*) as tweet_count,
    AVG(engagement_rate) as avg_engagement,
    SUM(likes) as total_likes,
    SUM(replies) as total_replies
FROM tweets_v2
WHERE DATE(posted_at) = CURRENT_DATE
GROUP BY pillar, pillar_name
ORDER BY pillar;

-- View: Quota status
CREATE OR REPLACE VIEW quota_status AS
SELECT 
    date,
    writes_executed,
    writes_reserved,
    (writes_executed + writes_reserved) as total_committed,
    (38 - writes_executed - writes_reserved) as slots_available,
    hard_capped,
    last_post_at
FROM api_quotas
WHERE date = CURRENT_DATE;

COMMENT ON SCHEMA twitter_bot IS 'Twitter Bot Pipeline V2 - Autonomous omnichannel media engine';
