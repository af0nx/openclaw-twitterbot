# SkinBetHub — CS2 Twitter Bot: Full Pipeline Architecture

> Autonomous CS2 Esports Media Engine
> Last updated: April 16, 2026
> **Status: LIVE — 15/15 PM2 services online | 10 tweets/day cap | configurable LLM tiers + Gemini Flash vision**

---

## LIVE SYSTEM STATE

| Component | Status | Detail |
|-----------|--------|--------|
| PM2 Processes | ✅ 15/15 online | See [PM2 Services](#2-pm2-services) table below |
| X API v2 Posting | ✅ LIVE | App `UbuntuOpenclaw`, Pay Per Use tier |
| ClawRouter | ✅ Running | `http://localhost:8402/v1` — 147+ models, systemd user service |
| Railway PostgreSQL | ✅ Connected | 18+ tables in `twitter_bot` schema, TLS enforced |
| LLM ECO | ✅ Configurable | `LLM_TIER_ECO` env — classification, urgency, dedup |
| LLM AUTO | ✅ Configurable | `LLM_TIER_AUTO` env + live A/B pools |
| LLM PREMIUM | ✅ Configurable | `LLM_TIER_PREMIUM` env + live A/B pools |
| LLM VISION | ✅ Paid | `google/gemini-2.5-flash` — screenshot analysis (bypasses ClawRouter → direct OpenRouter) |
| RSS Ingestion | ✅ 8 feeds | HLTV, Valve CS2, Esports Insider, Dust2, bo3.gg, Dexerto, GosuGamers, Esports.net |
| HLTV Monitor | ✅ Active | Match results, news, community comment scraping + vibe engine (~17 memes) |
| Twitter VIP Monitor | ✅ Active | Guest-token GraphQL, 20-tweet fetch, 2s stagger |
| Style Bank | ✅ 399 tweets | 10 target accounts scraped every 6h for few-shot prompting |
| Telegram HITL | ✅ Active | `@sbhtwitterbot` — Approve/Reject/Regenerate |
| Live Match Watcher | ✅ Active | Local trigger detection → burst screenshots → selective Gemini Flash vision |
| Community Liker | ✅ Active | 200 likes/day cap, human-like delays, routed via replies account |
| Tweet Pruner | ✅ Active | Deletes 0-engagement tweets after 48h |
| Fact Checker | ✅ Hardened | LLM + entity dicts, strict contradiction-only prompt |
| Card System | ✅ Active | 30 team colors, 56 player roles, 5 card types |
| Queue/State | ✅ Postgres-backed | Durable VIP dedup via DB existence checks and queued tweet polling from `tweets_v2` |
| Account Sharding | ✅ Active | `main` / `live` / `replies` posting buckets with per-account quotas |
| Bot Compose Stack | ✅ Ready | `Dockerfile.bot` + `docker-compose.bot.yml` for isolated bot deployment |
| Secrets | ✅ RAM-only | `/dev/shm/.env` — never on disk |

---

## TABLE OF CONTENTS

1. [Architecture Overview](#1-architecture-overview)
2. [PM2 Services](#2-pm2-services)
3. [Data Flow](#3-data-flow)
4. [Ingestion Layer](#4-ingestion-layer)
5. [Processing Layer](#5-processing-layer)
6. [Output Layer](#6-output-layer)
7. [Services Layer](#7-services-layer)
8. [Utilities](#8-utilities)
9. [Database Schema](#9-database-schema)
10. [LLM Routing](#10-llm-routing)
11. [The Persona](#11-the-persona)
12. [Card & Graphics System](#12-card--graphics-system)
13. [Vision Pipeline](#13-vision-pipeline)
14. [Safety & Guardrails](#14-safety--guardrails)
15. [File Map](#15-file-map)
16. [Prediction Modeling Guardrails](#16-prediction-modeling-guardrails)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INGESTION LAYER                             │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │
│  │ HLTV Monitor │  │  RSS Agg.    │  │ Twitter VIP  │              │
│  │ (matches +   │  │ (6 feeds)    │  │ (GraphQL     │              │
│  │  comments)   │  │              │  │  guest token) │              │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘              │
│         └────────────────┼────────────────────┘                     │
│                          ▼                                          │
│  ┌──────────────┐  ┌──────────┐  ┌──────────────┐                  │
│  │ Siftly       │  │ Clip     │  │ Style        │                  │
│  │ (Vision/OCR) │  │ Hunter   │  │ Scraper      │                  │
│  └──────┬───────┘  └────┬─────┘  └──────┬───────┘                  │
│         └────────────────┼───────────────┘                          │
│                          ▼                                          │
│               ┌──────────────────────┐                              │
│               │  PostgreSQL (Railway) │                              │
│               │  twitter_bot schema   │                              │
│               └──────────┬───────────┘                              │
│                          │                                          │
├──────────────────────────┼──────────────────────────────────────────┤
│                    PROCESSING LAYER                                  │
│                          ▼                                          │
│  ┌────────────────────────────────────────────────────────────┐     │
│  │               TWEET SCHEDULER (orchestrator)               │     │
│  │                                                            │     │
│  │  Event ──► ContentGenerator (3-agent Writer's Room)        │     │
│  │         │   ├─ Writer Agent (draft)                        │     │
│  │         │   ├─ Editor Agent (critique + approve)           │     │
│  │         │   └─ WhimsyInjector (personality pass)           │     │
│  │         │                                                  │     │
│  │         ├─► FactChecker (LLM + entity dicts)               │     │
│  │         ├─► ToneValidator (SVM + LLM fallback)             │     │
│  │         ├─► MiroFish Guard (100-agent swarm)               │     │
│  │         ├─► HashtagInjector (2-3 tags)                     │     │
│  │         ├─► MediaManager (images + bodyshots)              │     │
│  │         ├─► MemeGenerator (stat/VS/player cards)           │     │
│  │         ├─► ScreenshotAnalyzer (vision enrichment)         │     │
│  │         └─► MatchAnalyzer (deep post-match analysis)       │     │
│  └────────────────────────┬───────────────────────────────────┘     │
│                           │                                         │
├───────────────────────────┼─────────────────────────────────────────┤
│                     OUTPUT LAYER                                     │
│                           ▼                                         │
│  ┌─────────────┐  ┌─────────────┐  ┌────────────────┐              │
│  │ Auto-        │  │ Telegram    │  │ Peak Hour      │              │
│  │ Approve      │──│ HITL Gate   │──│ Scheduler      │              │
│  │ (trusted)    │  │ (human OK)  │  │ (15:00-23:00)  │              │
│  └──────┬──────┘  └──────┬──────┘  └───────┬────────┘              │
│         └────────────────┼──────────────────┘                       │
│                          ▼                                          │
│               ┌──────────────────┐                                  │
│               │  Twitter Poster  │                                  │
│               │  (X API v2)      │                                  │
│               └────────┬─────────┘                                  │
│                        │                                            │
│  ┌───────────┐ ┌──────┴──────┐ ┌───────────┐ ┌───────────────┐     │
│  │ Engagement │ │ Community   │ │ Tweet     │ │ Follower      │     │
│  │ Engine     │ │ Liker       │ │ Pruner    │ │ Growth        │     │
│  └───────────┘ └─────────────┘ └───────────┘ └───────────────┘     │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│                      SERVICES LAYER                                  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                LIVE MATCH WATCHER                             │   │
│  │  Twitch Screenshot (Playwright) ──► Gemini Flash Vision      │   │
│  │  ──► Highlight Detection ──► Narration ──► Auto-Tweet        │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. PM2 Services

15 always-on processes managed by PM2 via `ecosystem.config.js`:

| # | Service | Script | Mem Limit | Purpose |
|---|---------|--------|-----------|---------|
| 0 | `siftly_ingestor` | `ingestion/siftly_engine.py` | 800M | Vision/OCR analysis for VIP tweet images |
| 1 | `scrapling_pool` | `ingestion/ingestion_runner.py` | 500M | Runs HLTV + Twitter VIP + RSS in parallel |
| 2 | `tweet_scheduler` | `output/tweet_scheduler.py` | 300M | Central orchestrator — event→tweet pipeline |
| 3 | `vip_hitl_bot` | `output/vip_hitl_telegram.py` | 200M | Telegram bot for human approval |
| 4 | `scrapling_medic` | `utils/scrapling_medic.sh` | — | Auto-healing watchdog for scrapling_pool |
| 5 | `twitter_poster` | `output/twitter_poster.py` | 200M | Posts tweets via X API v2 |
| 6 | `engagement_tracker` | `output/engagement_tracker.py` | 150M | Fetches tweet metrics every 30 min |
| 7 | `follower_growth` | `output/follower_growth_tracker.py` | 150M | Tracks daily follower count |
| 8 | `prediction_webhook` | `ingestion/prediction_webhook.py` | 150M | Prediction results ingestion webhook |
| 9 | `style_scraper` | `ingestion/style_scraper.py` | 300M | Refreshes the style bank every 6 hours |
| 10 | `clip_hunter` | `ingestion/clip_hunter.py` | 400M | Discovers clips and reusable media assets |
| 11 | `tweet_pruner` | `output/tweet_pruner.py` | 100M | Deletes 0-engagement tweets after 48h |
| 12 | `engagement_engine` | `output/engagement_engine.py` | 1G | Generates proactive engagement opportunities |
| 13 | `community_liker` | `output/community_liker.py` | 150M | Likes CS2 tweets for relationship building |
| 14 | `live_watcher` | `services/live_match_watcher.py` | 800M | Live match screenshots → vision → narration |

All apps share `PYTHONPATH=__dirname` via `SHARED_ENV` in `ecosystem.config.js`, eliminating the need for `sys.path.insert` hacks.

**Commands:**
```bash
pm2 ls                              # Status of all services
pm2 logs <name> --lines 30          # View logs
pm2 restart <name>                  # Restart one service
pm2 reload ecosystem.config.js      # Reload all from config
pm2 monit                           # Live CPU/memory monitor
```

---

## 3. Data Flow

### Event Lifecycle

```
Source (HLTV / RSS / Twitter VIP)
  → events table (status: pending)
    → tweet_scheduler picks up event
      → content_generator.dual_agent_generate()  [3-agent Writer's Room]
        → fact_checker.check()                   [LLM + entity validation]
          → tone_validator.validate()            [anti-corporate filter]
            → mirofish_guard.evaluate()          [100-agent swarm vibe-check]
              → hashtag_injector.inject()         [2-3 relevant tags]
                → media_manager.find_media()      [images, bodyshots, cards]
                  → screenshot_analyzer (if live screenshot exists)
                    → DECISION:
                      ├─ Auto-approve (trusted source + high confidence)
                      │    → tweets_v2 (status: queued)
                      └─ HITL required → Telegram bot → human Approve/Reject
                           → tweets_v2 (status: queued)
                             → twitter_poster picks up queued tweets → POSTED
```

### Tweet Quota System

- **Daily cap:** 10 tweets/day (env: `DAILY_TWEET_CAP`)
- **Pre-commit reservation:** Scheduler reserves a slot in `api_quotas` before processing
- **Slot leak protection:** `try/finally` with `slot_consumed` flag — early returns always free the slot
- **Peak hour scheduling:** Non-urgent tweets deferred to 15:00–23:00 UTC (EU evening + NA afternoon)

---

### Optional TweetClaw Gateway Path

[TweetClaw](https://github.com/Xquik-dev/tweetclaw) is useful when this bot needs
X/Twitter reads and writes behind an OpenClaw tool boundary instead of spreading
raw X credentials across the monitor, poster, style, and media services. Keep it
optional: RSS, HLTV, Siftly, the Postgres queue, and Telegram HITL stay local.

### Install and verify

```bash
openclaw plugins install @xquik/tweetclaw
openclaw config set plugins.entries.tweetclaw.config.apiKey "$XQUIK_API_KEY"
openclaw config set tools.alsoAllow '["explore", "tweetclaw"]'
openclaw plugins inspect tweetclaw --runtime
```

Use the `explore` tool during startup or CI to discover current endpoint
contracts before wiring a service to `tweetclaw`. This keeps the bot from
hard-coding stale X/Twitter paths in long-running PM2 workers.

### Service mapping

| Current component | TweetClaw role | Keep this guardrail |
| --- | --- | --- |
| `scripts/ingestion/twitter_monitor.py` | Search tweets, search tweet replies, fetch timelines, and create monitors for VIP accounts | Deduplicate by tweet ID before LLM drafting and keep the 2s account stagger |
| `scripts/output/twitter_poster.py` | Post tweets, post tweet replies, and upload media after queue approval | Never bypass `vip_hitl_bot` for replies, DMs, follows, profile edits, or high-risk posts |
| `scripts/ingestion/style_scraper.py` | Refresh few-shot style examples from account timelines or user lookup responses | Store only tweet IDs, public text, metrics, and retrieval timestamps |
| `scripts/output/engagement_tracker.py` | Refresh tweet metrics and reply context for posted tweets | Persist normalized metrics only, not raw auth headers or private payloads |
| `scripts/processing/media_manager.py` | Use authenticated media upload and media download when source media requires it | Validate content type, byte size, and ownership before passing files into card or meme generation |
| `scripts/services/live_match_watcher.py` | Post urgent live-event drafts through the same approved queue path | Keep quota reservation and Telegram approval semantics unchanged |

### Runtime safety

- Store `XQUIK_API_KEY` only in OpenClaw local config or the process
  environment. Do not commit it, log it, send it to Telegram, or place it in LLM
  prompts.
- Log endpoint category, status, tweet IDs, and queue IDs. Do not log auth
  headers, direct-message bodies, private media URLs, or full request payloads.
- Treat tweet text, replies, profiles, media alt text, and external URLs as
  untrusted content before prompt construction.
- Keep writes behind explicit approval. Search, lookup, monitor, and metric
  refresh calls may run unattended within the quota system.
- If `tweetclaw` returns setup guidance or an auth error, mark the event
  retryable and alert operations instead of falling back to untracked direct
  credentials.
- Link for operators: <https://www.npmjs.com/package/@xquik/tweetclaw>.

---

## 4. Ingestion Layer

All files under `ingestion/`.

### `ingestion_runner.py` → PM2: `scrapling_pool`

Coordinator that runs all ingestion scripts in parallel via `asyncio.gather`:
- `hltv_monitor.py` — match results, news, community comments
- `twitter_monitor.py` — VIP tweet discovery
- `rss_aggregator.py` — CS2 news feeds

### `hltv_monitor.py` → via `scrapling_pool`

Scrapes HLTV.org via Scrapling `StealthySession` with Cloudflare bypass:
- **Match results:** T1 events only (ESL, BLAST, IEM, PGL, Major)
- **Community Vibe Engine:** Extracts comments from match pages, detects ~17 CS2 memes (EZ4ENCE, cry is free, s1mple GOAT, Liquid curse), measures per-team crowd sentiment
- Stores `community_vibe` JSONB in `events.metadata` for content generator context

### `rss_aggregator.py` → via `scrapling_pool`

**8 active RSS feeds** (polls every 120s):

| Feed | Source | CS2 Only? |
|------|--------|-----------|
| `hltv.org/rss/news` | HLTV | ✅ Pure |
| `store.steampowered.com/feeds/news/app/730` | Valve CS2 | ✅ Pure |
| `dust2.us/rss` | Dust2 | ✅ Pure |
| `bo3.gg/rss` | bo3.gg | ✅ Pure |
| `esportsinsider.com/feed` | Esports Insider | ❌ Filtered |
| `dexerto.com/feed` | Dexerto | ❌ Filtered |
| `gosugamers.net/counterstrike/rss` | GosuGamers CS | ✅ Pure |
| `esports.net/news/feed` | Esports.net | ❌ Filtered |

Mixed-esports feeds are filtered via CS2 keyword matching (primary: "cs2", "hltv", "faceit") plus spaCy entity extraction over team, player, event, and map names. Feed health is exported through Prometheus counters/gauges so dead feeds trigger alerts instead of silently degrading coverage.

### `twitter_monitor.py` → via `scrapling_pool`

Monitors VIP CS2 accounts via Twitter GraphQL guest-token API:
- `DynamicSession` with 4G proxy rotation
- 20 tweets per account, 2s stagger between accounts
- Auto-retry on >50% failure rate
- Local in-process hot cache plus durable Postgres dedup for already-seen VIP tweets

### `siftly_engine.py` → PM2: `siftly_ingestor`

Heavy vision analysis service (~1.7GB):
- OCR on charts/screenshots/memes in VIP tweet images
- BLIP captioning + pytesseract text extraction
- Sentence-transformer embeddings for semantic dedup
- Results stored in `siftly_events` table

### `clip_hunter.py` → PM2: `clip_hunter`

Multi-source media discovery:
- **Reddit:** `old.reddit.com` JSON API with bot-style UA (`cs2bot:v1.0`), r/cs2 + r/GlobalOffensive
- **HLTV galleries:** Photo scraping with Referer header
- **Twitch clips:** Embed URL extraction
- Native video prep: FFmpeg normalization + thumbnail generation for Twitter-ready clip assets
- Downloads images and video, stores in `media_library` table

### `style_scraper.py` → PM2: `style_scraper`

Scrapes 10 CS2 fan accounts every 6h for few-shot prompting:

| Account | Style |
|---------|-------|
| @Ozzny_CS2 | Dry humor, one-liners |
| @ThourCS2 | Fast news reaction |
| @CS2News_EN | Factual but punchy |
| @RazedEsport | Breaking news, hype |
| @BLASTPremier | Production moments |
| @HLTVorg | Stats, match results |
| @CScribe | Roster intel |
| @Slasher | Breaking esports news |
| @Dust2us | NA CS2 recaps |
| @s1mpleO | Pro player voice |

- Guest-token GraphQL API, filters viral-worthy tweets (engagement threshold)
- **Failure counter:** Accounts that fail 3+ times in a row are auto-skipped until next cycle
- 399 tweets in `style_bank` table

---

## 5. Processing Layer

All files under `processing/`.

### `openrouter_client.py` — LLM Routing

Unified LLM client with 4 tiers:

| Tier | Model | Cost | Used For |
|------|-------|------|----------|
| `eco` | `LLM_TIER_ECO` | Configurable | Classification, urgency, dedup, persona selection |
| `auto` | `LLM_TIER_AUTO` | Configurable | Tweet writing, VIP replies, thread composition |
| `premium` | `LLM_TIER_PREMIUM` | Configurable | Guardrails, tone validation, RLHF analysis |
| `vision` | `google/gemini-2.5-flash` | Paid | Screenshot analysis (direct OpenRouter, bypasses ClawRouter) |

- Text tiers route through ClawRouter at `localhost:8402`
- Vision tier creates separate `OpenAI(base_url='https://openrouter.ai/api/v1')` client (ClawRouter free models don't support multimodal)
- Automatic retry via `tenacity` with exponential backoff
- `AB_POOL_AUTO` and `AB_POOL_PREMIUM` are hot-reloaded from env every 5 minutes, so the weekly evaluator can shift traffic without a process restart

### `content_generator.py` — 3-Agent Writer's Room

1. **Writer Agent** — Generates initial draft with persona conditioning, community vibe context, episodic memory recall, and style bank few-shot examples
2. **Editor Agent (RealityChecker)** — Critiques draft for accuracy, tone, length; returns `APPROVED` or revision notes  
3. **WhimsyInjector** — Final personality pass; adds life to boring tweets or approves good ones

**Fast path:** Pillars 13-15 (engagement content) use 1 LLM call for speed. VIP replies (pillar 12) go through full 3-agent quality gate.

### `persona_classifier.py` — ML Persona Selection

LogisticRegression trained weekly on RLHF engagement data. Maps `{news_category, time_of_day, timeline_energy, topic_sentiment}` → best persona (Contrarian, Data Nerd, Degenerate, Insider). Falls back to rule-based selection if model unavailable.

### `episodic_memory.py` — Semantic Recall

pgvector-backed memory of past VIP interactions. Uses sentence-transformer embeddings + cosine similarity — when replying to an account, recalls past interactions for contextual references.

### `fact_checker.py` — Two-Layer Fact Checking

1. **Entity validation** — Known team names, player names, scores against dictionaries
2. **LLM verification** — Strict contradiction-only prompt. Metadata serialized as proper JSON. Only flags claims that DIRECTLY contradict the source.

### `tone_validator.py` — Anti-Corporate Filter

- Local TF-IDF + SVM classifier trained on approved/rejected tweets
- LLM fallback for edge cases
- Rejects tweets that sound like press releases, use fancy vocabulary, or read like a journalist

### `mirofish_guard.py` — Constitutional Judge Guard

Pre-tweet guardrail for high-risk content (hot takes, drama):
- Single LLM-as-judge pass with a compact constitutional rubric
- Focuses on defamation, harassment, compliance/gambling risk, and brand-safety escalation
- Returns structured JSON: risk score, concerns, rewrite hint, veto decision
- Same threshold model remains pillar-specific, but runtime cost is now one judge call instead of a simulated swarm

### `media_manager.py` — Image + Native Video Pipeline

- Extracts images from Steam/Valve/HLTV articles
- **HLTV player bodyshots:** 50+ players mapped to HLTV IDs, scrapes player pages for hero images
- **Team roster lookup:** Scrapes HLTV team pages for player lists
- Bounded caches (200 max) with auto-eviction
- Per-account Tweepy v1.1 upload clients (`main`, `live`, `replies`)
- Native video support: FFmpeg normalization + chunked `tweet_video` uploads
- Falls back to `meme_generator` for stat cards when no photo available

### `meme_generator.py` — Card Generation

Generates 1200×675px Twitter-optimized images with dark CS2 theme:

| Card Type | Method | Use Case |
|-----------|--------|----------|
| Match Result | `generate_match_result_card()` | Post-match scores; winner's team color on accent bar |
| VS Card | `generate_vs_card()` | Pre/post-match with team-colored names + winner indicators |
| Player Card | `generate_player_card()` | Statsmeister-style: bodyshot, big rating, stat grid, role badge |
| Hot Take | `generate_hot_take_card()` | Bold white text on dark background (screenshot-bait) |
| Stat Card | `generate_stat_card()` | K/D, ADR, rating comparison tables |

**30 Team Colors** — Brand hex codes: Spirit=#6B2FBF, NaVi=#FFDE00, FaZe=#E03C31, Vitality=#FFD700, G2=#E03C31, MOUZ=#E42313, Liquid=#003C71, Heroic=#FF6600, Astralis=#FF1E26, fnatic=#FF5900, Cloud9=#2F9FD5, ENCE=#FFD700, etc.

**56 Player Roles** — AWPer, IGL, Rifler, Entry, Support. Examples: donk=Rifler, s1mple=AWPer, karrigan=IGL, ropz=Rifler, ZywOo=AWPer. Displayed on player cards as "SPIRIT · RIFLER".

### `screenshot_analyzer.py` — AI Vision + Data Graphics

Powered by Gemini 2.5 Flash:
- **`analyze_screenshot()`** — Extracts full game state JSON (teams, scores, round, economy, players alive, weapons, tactical situation)
- **`detect_highlight_moment()`** — Identifies: clutch situations, match points, overtime, eco upsets, comebacks
- **`generate_live_narration()`** — Hype commentary ("THIS IS FOR MATCH POINT! 🔥")
- **`_clean_narration()`** — Robust chain-of-thought stripping (XML `<thinking>` tags, regex CoT detection, quoted text fallback, hashtag removal)
- **`DataGraphicsGenerator`** — MischiefCS2-style infographics: ranking tables, major race, team form, map stats

### `twitch_screenshotter.py` — Live Stream Capture

Headless Playwright (Chromium) captures live Twitch streams:
- **26 tournament keywords** mapped to channels (ESL→eslcs, BLAST→blast, PGL→pgl, Major→eslcs/pgl/blast, Perfect World→pwrdcs, BetBoom→ruhub_cs, etc.)
- **7 fallback streams:** eslcs, blast, pgl, casthouse_cs2, elozhell, faceit, ruhub_cs
- 1920×1080 capture, crops to game area
- Per-channel cooldown to avoid duplicate screenshots
- Auto-bypasses Twitch mature content gate

### Other Processing Modules

| File | Purpose |
|------|---------|
| `hashtag_injector.py` | Pure-function: injects 2-3 CS2 hashtags (#CS2, event-specific) |
| `match_analyzer.py` | Deep post-match analysis — upset detection, prediction tracking, auto-threads for big results |
| `rlhf_tuner.py` | Weekly auto-learning — updates system prompt appendix, semantic drift detection (cosine sim >0.70 from base) |

---

## 6. Output Layer

All files under `output/`.

### `tweet_scheduler.py` → PM2: `tweet_scheduler`

The central brain (~780MB). Orchestrates the full event→tweet pipeline:
- Polls `events` table for unprocessed events
- Routes through: Content Gen → Fact Check → Tone → MiroFish → Hashtags → Media
- **Vision enrichment:** If event has `_screenshot_path`, runs AI vision analysis; appends narration to highlight tweets
- **Quota management:** Pre-commit reservation per account bucket with `try/finally` leak protection
- **Account routing:** Stores `account_bucket` on every queued tweet (`main`, `live`, `replies`)
- **Queue handoff:** Marks tweets `queued`; poster service picks them up directly from Postgres
- **Auto-approve:** High-confidence news from trusted sources skips HITL
- **Peak hours:** Non-urgent tweets deferred to 15:00–23:00 UTC
- **T1 filter:** Only processes Tier 1 events (major tournaments, top teams)

### `twitter_poster.py` → PM2: `twitter_poster`

Executes postings via X API v2 (Tweepy):
- Supports: single tweets, replies, quote tweets, threads, media_ids
- Per-account clients for `main`, `live`, and `replies`
- Polls `tweets_v2` for ready queued work each cycle
- Per-account quota execution tracking (`account_quotas`) plus legacy aggregate tracking (`api_quotas`)
- Marks `tweets_v2` rows as `posted` with tweet_id after successful post

### `vip_hitl_telegram.py` → PM2: `vip_hitl_bot`

Telegram bot `@sbhtwitterbot` for human-in-the-loop:
- Shows tweet preview with media/thread/QT badges
- Buttons: Approve ✅ / Reject ❌ / Regenerate 🔄
- Auto-approved news bypasses this gate

### `engagement_engine.py` → PM2: `engagement_engine`

Growth machine (~71MB):
- Trend riding (CS2 trending topics)
- Ratio hunting (quote-tweet bad takes)
- Poll generation (AWPer debates, team predictions)
- Clip reactions (viral clip commentary)
- First-responder replies (fast replies to breaking news)
- Conversation starters (community engagement)

### `community_liker.py` → PM2: `community_liker`

Proactive engagement:
- Likes replies to own tweets, VIP tweets, CS2 keyword search results
- **200 likes/day** cap, 3-8s human-like delays
- Auth now uses the replies account shard for engagement actions
- Tracks in `community_likes` + `like_quotas` tables

### `engagement_tracker.py` → PM2: `engagement_tracker`

Metrics pipeline:
- Fetches tweet metrics every 30 min via Tweepy bearer token
- Stores engagement snapshots in `engagement_tracking`
- Auto-labels tweets as top/good/mid for RLHF feedback

### `tweet_pruner.py` → PM2: `tweet_pruner`

Garbage collection:
- Deletes tweets older than 48h with 0 likes and 0 replies
- Max 20 deletions per cycle
- Improves average engagement rate

### `follower_growth_tracker.py` → PM2: `follower_growth`

Tracks follower count every 60 min → `follower_snapshots` table.

### `thread_composer.py`

Library for composing multi-tweet threads (Pillar 10 daily threads, Valve update breakdowns). Persona-conditioned LLM generation with intelligent segmentation into tweet-length chunks.

---

## 7. Services Layer

### `services/live_match_watcher.py`

The bot's "eyes" (~77MB):

```
Every ~12s monitor sweep:
  1. Query DB for live T1 matches (events from last 4 hours)
  2. Reuse persistent Playwright stream pages per channel
  3. Run local OpenCV/OCR trigger detection over score/killfeed/center regions
  4. Capture a burst only when action spikes
  5. Send burst frames to Gemini Flash selectively
  6. Compare against last known state (per-match tracking)
  7. Generate hype narration and queue under the `live` account bucket
```

**Rate limiting:**
- Max 4 tweets per match
- 3 min cooldown between tweets
- Per-match state tracking with 6h TTL (auto-cleanup of stale matches)
- DB reconnection with exponential backoff (1s → 60s)

---

## 8. Utilities

All files under `utils/`.

| File | Purpose |
|------|---------|
| `db_utils.py` | Shared PostgreSQL auto-reconnect wrapper (`ensure_db_connection`) — enforces `search_path=twitter_bot,public` at connection level |
| `config.py` | Centralized config loader — single `load_config()` replaces per-file `load_dotenv` calls; helpers: `get_database_url()`, `get_openrouter_key()`, `get_env()` |
| `observability.py` | Sentry error tracking + Prometheus metrics singleton — tweets_posted, llm_latency, vision_calls, openrouter_spend; `/metrics` on `:9100` |
| `signal_utils.py` | SIGTERM/SIGINT helpers so PM2 stops long-running daemons cleanly |
| `cs2_constants.py` | Centralized keyword sets — teams, events, maps, players, roles. Used by ingestion, scheduling, fact-checking |
| `twitter_accounts.py` | Bucket routing + per-account credential resolution (`main`, `live`, `replies`) |
| `account_quota.py` | Per-account reservation/release/increment helpers backed by `account_quotas` |
| `runtime_schema.py` | Idempotent runtime bootstrap for `account_quotas`, `tweets_v2.account_bucket`, and VIP dedup indexes |
| `health_monitor.py` | Shadowban canary — posts test tweets, verifies visibility from diverse geographic IPs |
| `analytics_tracker.py` | Engagement metrics for RLHF feedback (6h poll cycle via X API) |

### Processing: `entity_layer.py` — spaCy Entity Extraction

Shared NLP pipeline replacing three divergent keyword implementations:
- `get_nlp()` — singleton spaCy `EntityRuler` loaded from `data/cs2_entities.jsonl` (113 patterns: 39 teams, 43 players, 20 events, 9 maps)
- `extract_entities(text)` — returns `{teams, players, events, maps}` sets
- `is_cs2_relevant(text)` — boolean relevance check (replaces ad-hoc keyword lists in `fact_checker.py`, `rss_aggregator.py`, `twitter_monitor.py`)
- `entity_summary(text)` — one-line human-readable summary

---

## 9. Database Schema

**Railway PostgreSQL 16** — `twitter_bot` schema, core tables plus runtime-safe sharding extensions:

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `events` | All ingested events (480 rows) | `headline`, `content`, `source`, `category`, `urgency`, `metadata` (JSONB), `status` |
| `tweets_v2` | Tweet drafts + posted tweets (375 rows) | `pillar`, `content`, `status` (draft/queued/posted/rejected), `media_path`, `twitter_tweet_id`, `account_bucket` |
| `api_quotas` | Legacy aggregate daily tweet quota tracking | `date`, `writes_executed`, `writes_reserved` |
| `account_quotas` | Per-account quota tracking for sharded posting | `date`, `account_bucket`, `writes_executed`, `writes_reserved` |
| `style_bank` | Viral tweet examples for few-shot (399 rows) | `tweet_id`, `source_account`, `text`, `likes`, `retweets` |
| `engagement_tracking` | Per-tweet engagement snapshots | `tweet_id`, `likes`, `retweets`, `replies`, `impressions` |
| `follower_snapshots` | Hourly follower count | `timestamp`, `follower_count` |
| `community_likes` | Likes given by community_liker | `tweet_id`, `liked_at` |
| `like_quotas` | Daily like quota tracking | `date`, `likes_given` |
| `media_library` | Downloaded clips/images from clip_hunter | `hash`, `path`, `source`, `type` |
| `siftly_events` | Vision analysis results | `event_id`, `analysis` (JSONB) |
| `monitored_accounts` | VIP Twitter accounts to watch | `username`, `user_id` |
| `rlhf_history` | Weekly RLHF tuning results | `week`, `top_tweets`, `bottom_tweets`, `appendix` |
| `hitl_engaged_7d` | HITL engagement tracking | `date`, `engaged_count` |
| `canary_logs` | Shadowban detection logs | `tweet_id`, `visible`, `checked_at` |
| `reddit_posts` | (Disabled) Reddit cross-post tracking | — |
| `system_metrics` | Service health metrics | `service`, `metric`, `value`, `timestamp` |
| `todays_performance` | Daily performance summary | `date`, aggregated metrics |
| `quota_status` | Quota monitoring view | — |

**Connection:** `DATABASE_URL` env var → `postgresql://...railway.app:5432/...` with TLS. Connection-level `search_path=twitter_bot,public` enforced in `db_utils.py`.

---

## 10. LLM Routing

```
┌──────────────────┐     ┌──────────────┐     ┌──────────────────┐
│  Python Scripts   │────►│  ClawRouter   │────►│  OpenRouter      │
│  (OpenAI SDK)     │     │  :8402/v1     │     │  (free models)   │
└──────────────────┘     └──────────────┘     └──────────────────┘

┌──────────────────┐                          ┌──────────────────┐
│  Vision calls     │─────────────────────────►│  OpenRouter      │
│  (direct client)  │  (bypasses ClawRouter)   │  (Gemini Flash)  │
└──────────────────┘                          └──────────────────┘
```

**Why bypass ClawRouter for vision?** ClawRouter $0 balance → falls back to free models → free models don't support multimodal. Vision calls go directly to OpenRouter API with the `OPENROUTER_API_KEY`.

**Cost structure:** All text generation is free. Only vision calls (Gemini 2.5 Flash) cost money — rate-limited to live match highlights.

**If ClawRouter crashes:**
```bash
systemctl --user restart openclaw-gateway.service
```

---

## 11. The Persona

> **"The HLTV Regular Who Reads Bloomberg"**

- **Voice:** Sharp, irreverent, HLTV-native. Never corporate. Never cringe.
- **Humor:** Dry. Knows EZ4ENCE, s1mple GOAT debate, Liquid curse, cry is free — uses them when relevant, not forced.
- **Intelligence:** Knows H2H records, map stats, team form. Drops facts casually.
- **Community-Aware:** Reads HLTV comments to absorb vibes before generating tweets.
- **Speed:** First to post breaking CS2 news.
- **Language:** B2 English only. Simple words. No fancy vocabulary. No em-dashes.

**Core Constraints:**
1. Exclusively CS2 — no generic gaming, no betting spam, no poker
2. 10 tweets/day API cap — every tweet must be high-value
3. No browser automation for posting — strictly X API v2
4. HITL Telegram gate for risky content
5. Weekly RLHF self-correction with semantic drift ceiling (cosine sim >0.70)
6. All LLM calls through ClawRouter (except vision)

---

## 12. Card & Graphics System

### Card Types

Dark theme, 1200×675px, @SkinBetHub watermark on all cards:

| Card Type | Method | Example |
|-----------|--------|---------|
| Match Result | `generate_match_result_card(team1, team2, score1, score2, event)` | SPIRIT 2-1 NAVI with purple accent bar |
| VS Card | `generate_vs_card(team1, team2, score1, score2, event, map)` | FaZe (red) vs Vitality (gold), score centered |
| Player Card | `generate_player_card(player, stats, event, team)` | DONK: 1.52 rating, bodyshot, SPIRIT · RIFLER |
| Hot Take | `generate_hot_take_card(text)` | Bold white text on dark background |
| Stat Card | `generate_stat_card(title, stats)` | K/D, ADR comparison grid |

### Team Colors (30 teams)

| Team | Color | Team | Color |
|------|-------|------|-------|
| Spirit | `#6B2FBF` (purple) | NaVi | `#FFDE00` (yellow) |
| FaZe | `#E03C31` (red) | Vitality | `#FFD700` (gold) |
| G2 | `#E03C31` (red) | MOUZ | `#E42313` (red) |
| Liquid | `#003C71` (navy) | Heroic | `#FF6600` (orange) |
| Astralis | `#FF1E26` (red) | fnatic | `#FF5900` (orange) |
| Cloud9 | `#2F9FD5` (blue) | ENCE | `#FFD700` (gold) |
| FURIA | `#333333` (dark) | Complexity | `#E4002B` (red) |
| Eternal Fire | `#FF0000` (red) | The MongolZ | `#1E4D8C` (blue) |

*Unknown teams fall back to accent yellow (#E8B849).*

### Player Roles (56 players)

Roles shown on player cards. Examples:
```
AWPer:   s1mple, ZywOo, m0NESY, broky, sh1ro, device, torzsi
IGL:     karrigan, chopper, aleksib, gla1ve, siuhy, cadian, nexa
Rifler:  donk, NiKo, ropz, frozen, EliGE, xyp9x, stavn, electronic
Entry:   YEKINDAR
Support: mezii, xyp9x
```

### Data Graphics (via `DataGraphicsGenerator`)

MischiefCS2-style infographics generated from vision analysis data:
- Ranking tables (team standings)
- Major race visualizations
- Team form charts
- Map statistics

---

## 13. Vision Pipeline

The bot has "eyes" — it can SEE live CS2 matches and narrate them:

```
Twitch Stream
    │
    ▼
Playwright persistent channel page
  │
  ▼
Local action detector (OpenCV region diffs + optional OCR keywords)
  │  cheap trigger gate: only escalate on action spikes
    │
    ▼
Gemini 2.5 Flash (vision tier)
    │  Structured JSON output:
    │  {teams, scores, round, economy,
    │   players_alive, weapons, tactical_situation}
    ▼
Highlight Detection
    │  clutch? match_point? overtime?
    │  eco_upset? comeback?
    ▼
Narration Generation
    │  "THIS IS FOR MATCH POINT! 🔥"
    │  (cleaned via _clean_narration — strips CoT)
    ▼
Tweet with screenshot attached
```

### Tournament → Stream Mapping (26 keywords)

| Keyword | Twitch Channels |
|---------|----------------|
| ESL / IEM | eslcs, elozhell |
| BLAST | blast, blastpremier |
| PGL | pgl, pgl_csgo |
| Major | eslcs, pgl, blast |
| FACEIT | faceit, casthouse_cs2 |
| Perfect World / PWR | pwrdcs |
| BetBoom | ruhub_cs, paboron |
| Thunderpick | thunderpickesports |
| Elisa | elisaesports |
| Skyesports | skyesportsindia |

**7 fallback streams** (tried when no keyword match): eslcs, blast, pgl, casthouse_cs2, elozhell, faceit, ruhub_cs

### Vision Enrichment in Tweet Scheduler

When the media pipeline captures a Twitch screenshot (`event['_screenshot_path']`), the scheduler:
1. Runs `analyze_screenshot()` → game state JSON
2. Runs `detect_highlight_moment()` → urgency level
3. If high urgency → `generate_live_narration()` → prepends `📺 {narration}` to tweet text

The live watcher no longer sends every blind polling frame to Gemini. It first runs a local trigger loop, captures a short burst on action, then only spends vision calls on those burst frames.

---

## 14. Safety & Guardrails

### Pre-Post Quality Pipeline

Every tweet passes through (in order):
1. **FactChecker** — LLM + entity dicts; strict contradiction-only (won't flag missing info)
2. **ToneValidator** — SVM classifier + LLM; rejects corporate tone, press-release style
3. **MiroFish Guard** — constitutional judge; vetoes on pillar-specific risk thresholds
4. **HITL Gate** — Telegram approval for non-auto-approved content

### Quota Protection

- Daily caps enforced per account bucket via `account_quotas`
- Aggregate totals still mirrored into `api_quotas` for dashboard/backward compatibility
- Pre-commit slot reservation (reserve before processing, release on failure)
- `try/finally` with `slot_consumed` flag prevents leaked slots
- Peak-hour deferral for non-urgent content (15:00–23:00 UTC)

### Content Safety

- No betting/gambling promotion in tweet text
- No financial advice
- No personal attacks on players
- CS2-only content filter rejects off-topic events
- RLHF drift ceiling: cosine similarity must stay >0.70 from base prompt

### Infrastructure Safety

- Secrets in RAM only (`/dev/shm/.env`) — never on disk
- Railway PostgreSQL with TLS (`sslmode=verify-full`)
- Postgres keeps VIP dedup durable across restarts through indexed existence checks, and queued tweet delivery is DB-driven
- `Dockerfile.bot` and `docker-compose.bot.yml` isolate bot services from the main app stack and let heavy workers run behind compose profiles
- Rate-limited scraping with exponential backoff on all external requests
- Bounded caches (200 max entries) prevent memory leaks
- Style scraper auto-skips dead accounts after 3 consecutive failures
- Live watcher cleans up stale match state after 6 hours

---

## 15. File Map

```
skinbethub_twitter/
├── ecosystem.config.js                 # PM2 config (9 services, SHARED_ENV with PYTHONPATH)
├── pyproject.toml                      # Python project metadata, pytest config, Ruff linter
├── TWITTER_BOT_PIPELINE.md             # This architecture doc
├── schema.sql                          # Database schema definitions
├── requirements.txt                    # Python dependencies
│
├── ingestion/                          # ── DATA IN ──
│   ├── __init__.py
│   ├── ingestion_runner.py             #   Coordinator (PM2: scrapling_pool)
│   ├── hltv_monitor.py                 #   HLTV scraping + community vibe engine
│   ├── twitter_monitor.py              #   VIP tweet discovery (GraphQL guest token)
│   ├── rss_aggregator.py               #   6 RSS feeds, CS2 keyword filtering
│   ├── siftly_engine.py                #   Vision/OCR for tweet images (PM2: siftly_ingestor)
│   ├── clip_hunter.py                  #   Reddit + HLTV + Twitch clips
│   ├── style_scraper.py                #   10 fan accounts → style bank
│   └── prediction_webhook.py           #   Prediction results webhook (PM2: prediction_webhook)
│
├── processing/                         # ── TRANSFORM ──
│   ├── __init__.py
│   ├── openrouter_client.py            #   LLM routing (4 tiers: eco/auto/premium/vision)
│   ├── content_generator.py            #   3-agent Writer's Room (Writer → Editor → Whimsy)
│   ├── persona_classifier.py           #   ML persona selection (LogisticRegression on RLHF)
│   ├── episodic_memory.py              #   pgvector semantic recall of past interactions
│   ├── fact_checker.py                 #   LLM + entity fact checking (contradiction-only)
│   ├── entity_layer.py                 #   spaCy EntityRuler — shared entity extraction (113 patterns)
│   ├── tone_validator.py               #   Anti-corporate SVM + LLM filter
│   ├── mirofish_guard.py               #   Constitutional judge guard
│   ├── hashtag_injector.py             #   Auto-injects 2-3 CS2 hashtags
│   ├── media_manager.py                #   Image pipeline + HLTV bodyshots (50+ players)
│   ├── meme_generator.py               #   Card generation (30 team colors, 56 player roles)
│   ├── match_analyzer.py               #   Deep post-match analysis + predictions
│   ├── screenshot_analyzer.py          #   AI vision (Gemini Flash) + data graphics
│   ├── twitch_screenshotter.py         #   Live stream capture (Playwright, 26 stream mappings)
│   └── rlhf_tuner.py                  #   Weekly auto-learning with drift detection
│
├── output/                             # ── DATA OUT ──
│   ├── __init__.py
│   ├── tweet_scheduler.py              #   Central orchestrator (PM2: tweet_scheduler)
│   ├── twitter_poster.py               #   X API v2 posting (PM2: twitter_poster)
│   ├── vip_hitl_telegram.py            #   Telegram HITL bot (PM2: vip_hitl_bot)
│   ├── engagement_engine.py            #   Growth: polls, trends, ratios
│   ├── engagement_tracker.py           #   Metrics every 30 min (PM2: engagement_tracker)
│   ├── community_liker.py              #   200 likes/day proactive
│   ├── follower_growth_tracker.py      #   Hourly follower tracking (PM2: follower_growth)
│   ├── tweet_pruner.py                 #   0-engagement cleanup
│   ├── thread_composer.py              #   Multi-tweet thread builder
│   ├── prediction_results.py           #   Prediction resolution
│   └── reddit_cross_poster.py          #   (DISABLED — Reddit policy violation)
│
├── services/                           # ── LONG-RUNNING ──
│   └── live_match_watcher.py           #   Vision narration (PM2: live_watcher)
│
├── utils/                              # ── SHARED ──
│   ├── __init__.py
│   ├── config.py                       #   Centralized config loader (replaces per-file load_dotenv)
│   ├── observability.py                #   Sentry + Prometheus metrics (:9100)
│   ├── db_utils.py                     #   PostgreSQL auto-reconnect (search_path=twitter_bot)
│   ├── signal_utils.py                 #   SIGTERM/SIGINT helpers for PM2 shutdown
│   ├── cs2_constants.py                #   Teams, events, maps, players
│   ├── twitter_accounts.py             #   Account bucket routing
│   ├── account_quota.py                #   Per-account quota helpers
│   ├── runtime_schema.py               #   Runtime schema bootstrap
│   ├── health_monitor.py               #   Shadowban canary
│   ├── analytics_tracker.py            #   Engagement metrics for RLHF
│   └── scrapling_medic.sh              #   Auto-healing watchdog (PM2: scrapling_medic)
│
├── scripts/
│   ├── run_migrations.py               #   Applies new SQL migrations once
│   ├── backup_database.sh              #   Daily logical backup helper
│   └── refresh_cs2_entities.py         #   Quarterly entity refresh from constants
│
├── ops/
│   └── prometheus/prometheus.yml       #   Example scrape config for :9100 metrics
│
├── tests/                              # ── TEST SUITE (140 tests) ──
│   ├── conftest.py                     #   Shared fixtures
│   ├── test_database_roundtrip.py      #   Integration smoke test for DATABASE_URL
│   ├── test_rss_filtering.py           #   RSS feed CS2 filtering
│   ├── test_fact_checker.py            #   Fact-checker pipeline
│   ├── test_cs2_filtering.py           #   CS2 relevance detection
│   ├── test_scheduling_rules.py        #   Quota + scheduling logic
│   ├── test_meme_generator.py          #   Card generation
│   ├── test_entity_layer.py            #   spaCy entity extraction
│   ├── test_observability.py           #   Prometheus metrics
│   └── test_schema_isolation.py        #   DB schema search_path
│
├── data/
│   ├── cs2_entities.jsonl              # spaCy EntityRuler patterns (113 entries)
│   ├── generated_images/               # MemeGenerator card output
│   ├── twitch_screenshots/             # Live match screenshots
│   └── clips/                          # Downloaded Reddit/HLTV media
│
├── migrations/
│   └── 001_add_missing_tables.sql      # Adds style_bank, media_library, follower_snapshots, community_likes, like_quotas
│   └── 002_update_legacy_quota_cap.sql # Aligns legacy api_quotas helper to 10/day
│
├── logs/                               # PM2 log files (per-service)
├── models/                             # ML model weights (persona classifier, etc.)
├── config/
│   └── .env.example                    # Env template (secrets live in /dev/shm/.env)
└── docs/
    └── railway-db-setup.md             # Railway PostgreSQL setup guide
```

---

## 16. Prediction Modeling Guardrails

The current production prediction layer is intentionally smaller than the broader research stack sometimes discussed around CS2 modeling.

| Topic | Current Repo Truth | Guardrail |
|---|---|---|
| Rating layer | `scripts/processing/team_rating_engine.py` is a confidence-shrunk Elo-style team prior rebuilt from `match_result` events | Do not describe it as a player-level Glicko-2 map-side system. `7 maps × 2 sides = 14` streams per player, not 70 rating systems. `70` only makes sense as a 5-player feature dimension. |
| Pre-match vs live | `match_prediction` events are pre-match picks; `services/live_match_watcher.py` is a separate live screenshot + vision path | Do not compare pre-match match-winner metrics to in-round round-win or tactical-classification metrics as if they are the same benchmark. |
| THGNN evidence | The Sloan 2025 THGNN paper supports live round-state valuation and action attribution | Treat it as evidence for a future live model, not as proof of pre-match betting accuracy. |
| Calibration evidence | Walsh & Joshi, `arXiv:2303.06021`, is the primary calibration and Kelly-sizing source in this stack | Use it for calibration, implied-probability baselines, and bankroll-policy discussion, not for CS2-specific architecture claims. |
| PandaScore | PandaScore documents a Markov Binomial in-play round-dynamics model | Do not label it as a transformer or GNN deployment. |

- Market-odds features must be timestamped strictly before every downstream label. Training on market odds and then scoring edge against that same closing market without a market-only baseline is circular.
- Feature counts must be explicit. Do not use loose phrases like `25-feature vector` unless every field and construction timestamp is enumerated.
- Kelly fraction, EV threshold, and bankroll caps are tuned hyperparameters, not fixed conclusions from the cited papers.
- Every future model claim in docs or code should name the task, label, metric, evidence quality, and source URL.

Primary references:
- THGNN round-state paper: https://www.sloansportsconference.com/research-papers/evaluating-player-actions-in-professional-counter-strike-using-temporal-heterogeneous-graph-neural-networks
- Calibration paper: https://arxiv.org/abs/2303.06021
- PandaScore round model: https://www.pandascore.co/blog/cs-go-round-modeling

**END OF PIPELINE ARCHITECTURE**

Last reviewed: April 16, 2026 — prediction-modeling guardrails updated. Full pipeline inventory last verified April 13, 2026.
Codebase: 44+ Python files · 23 DB tables · 4 LLM tiers · 30 team colors · 56 player roles · 26 stream mappings · 113 spaCy entity patterns.
Text generation: env-configured tiers with live A/B pools. Vision: Gemini Flash (paid, rate-limited to highlights only).
