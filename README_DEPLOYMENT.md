# Twitter Bot Pipeline V2 - Deployment Guide

## 🚀 Quick Start

This is the complete autonomous Twitter Bot Pipeline V2 for 21Core AI - "The Degenerate Bloomberg"

### Architecture Overview
- **Ingestion Layer**: 4G/5G mobile proxy scraping (Scrapling framework) + 8 RSS feeds
- **Processing Layer**: 3-agent LLM generation, spaCy entity extraction, MiroFish safety, RLHF tuning
- **Output Layer**: X API v2 posting with strict 100/day quota enforcement
- **Utilities**: Analytics tracking, shadowban monitoring, Prometheus metrics, auto-healing

---

## 📋 Prerequisites

### System Requirements
- **VPS**: 12GB RAM dedicated, 8+ cores (Hetzner recommended)
- **OS**: Ubuntu 22.04+ with Docker
- **Node.js**: 18+ (for PM2)
- **Python**: 3.11+
- **PostgreSQL**: Railway PostgreSQL 16 (managed, shared instance)

### External Services
- X/Twitter API v2 Basic Tier ($100/month)
- OpenRouter API key (LLM routing)
- Telegram Bot (HITL gateway)
- Mobile proxy provider (4G/5G API-controlled)
- Railway account (PostgreSQL hosting)

---

## 🔧 Installation

### 1. Clone and Setup

```bash
cd /home/ubuntu/skinbethub_twitter

# Install Python dependencies
pip3 install -r requirements.txt

# Install Node.js and PM2
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g pm2

# Install Docker (for sandbox testing)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Install security tools
pip3 install bandit
```

### 2. Database Setup

```bash
# Connect to Railway PostgreSQL
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"

# Execute schema
psql $DATABASE_URL < schema.sql
python3 scripts/run_migrations.py

# Verify tables
psql $DATABASE_URL -c "SELECT COUNT(*) FROM twitter_bot.events"
```

### 3. Secrets Configuration

```bash
# Install Mozilla sops for secrets encryption
# (Follow sops installation guide)

# Decrypt secrets to tmpfs
sops --decrypt config/.env.enc > /dev/shm/.env

# Verify required secrets
cat /dev/shm/.env | grep -E "^(DATABASE_URL|OPENROUTER_API_KEY|X_API_KEY|TELEGRAM_BOT_TOKEN)"

# Auto-delete after 5 seconds (PM2 loads first)
(sleep 5 && rm /dev/shm/.env) &
```

**Required secrets in `.env`:**
```bash
# Database
DATABASE_URL=postgresql://...railway.app:5432/...

# OpenRouter LLM
OPENROUTER_API_KEY=sk-or-...

# X/Twitter API v2
X_API_KEY=...
X_API_SECRET=...
X_ACCESS_TOKEN=...
X_ACCESS_SECRET=...
X_BEARER_TOKEN=...

# Telegram HITL
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321

# Reddit (optional)
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_USERNAME=...
REDDIT_PASSWORD=...

# Mobile Proxies
MOBILE_PROXY_1=http://proxy1:port
MOBILE_PROXY_2=http://proxy2:port

# Modes
DRY_RUN_MODE=false
HITL_TIMEOUT_HOURS=4
```

### 4. Start Services

```bash
# Start PM2 daemons
pm2 start ecosystem.config.js

# Verify all processes running
pm2 status

# Watch logs
pm2 logs --lines 20
```

---

## 📂 Project Structure

```
/home/ubuntu/skinbethub_twitter/
├── ingestion/
│   ├── rss_aggregator.py        # 8 RSS feeds (120s cycle)
│   ├── twitter_monitor.py       # VIP account monitoring
│   ├── hltv_monitor.py          # CS2 match results
│   ├── siftly_engine.py         # Vision OCR analysis
│   ├── prediction_webhook.py    # Prediction results webhook
│   ├── clip_hunter.py           # Viral media discovery
│   ├── style_scraper.py         # Style bank refresh
│   └── ingestion_runner.py      # Parallel coordinator
├── processing/
│   ├── openrouter_client.py     # Unified LLM API
│   ├── content_generator.py     # 3-agent writer's room
│   ├── entity_layer.py          # spaCy entity extraction (113 patterns)
│   ├── mirofish_guard.py        # Constitutional judge guard
│   ├── fact_checker.py          # Hallucination prevention
│   ├── tone_validator.py        # Compliance checker
│   └── rlhf_tuner.py            # Weekly auto-learning
├── output/
│   ├── tweet_scheduler.py       # Central queue + quota
│   ├── twitter_poster.py        # X API v2 execution
│   ├── vip_hitl_telegram.py     # Telegram approval bot
│   ├── thread_composer.py       # Daily thread generator
│   ├── engagement_tracker.py    # tweet metrics polling
│   ├── engagement_engine.py     # proactive engagement ideas
│   ├── community_liker.py       # relationship-building likes
│   └── tweet_pruner.py          # low-engagement cleanup
├── services/
│   └── live_match_watcher.py    # screenshot-driven live commentary
├── utils/
│   ├── config.py                # Centralized config loader
│   ├── observability.py         # Sentry + Prometheus metrics
│   ├── db_utils.py              # PostgreSQL auto-reconnect
│   ├── signal_utils.py          # SIGTERM/SIGINT helpers
│   ├── analytics_tracker.py     # Engagement metrics (6h)
│   ├── health_monitor.py        # Shadowban canary (6h)
│   └── scrapling_medic.sh       # Auto-healing daemon
├── scripts/
│   ├── run_migrations.py        # schema migration runner
│   ├── backup_database.sh       # daily logical backup script
│   └── refresh_cs2_entities.py  # quarterly entity refresh
├── ops/
│   └── prometheus/prometheus.yml # scrape config for :9100 metrics
├── .github/workflows/
│   └── ci.yml                   # pytest + Ruff CI
├── tests/                       # 139 unit tests (pytest)
├── data/                        # spaCy patterns, generated images
├── migrations/                  # SQL migration scripts
├── config/
│   └── .env.example             # Template
├── models/                      # Siftly model weights
├── logs/                        # PM2 logs
├── backup/                      # DB dumps
├── schema.sql                   # PostgreSQL schema
├── ecosystem.config.js          # PM2 configuration (15 services)
├── pyproject.toml               # Python project config
├── requirements.txt             # Python dependencies
└── README_DEPLOYMENT.md         # This file
```

---

## 🎯 Deployment Phases

### Phase 1: Silent Listen (Days 1-7)
**Goal**: Validate ingestion without posting

```bash
# Set dry-run mode
export DRY_RUN_MODE=true

# Start only ingestion + processing
pm2 start ecosystem.config.js --only siftly_ingestor,scrapling_pool

# Monitor Siftly data
psql $DATABASE_URL -c "SELECT COUNT(*), category FROM twitter_bot.events GROUP BY category"

# Check LLM generation (no posting)
pm2 logs tweet_scheduler
```

### Phase 2: Restricted Output (Days 8-14)
**Goal**: Limited posting with full HITL review

```bash
# Disable dry-run
export DRY_RUN_MODE=false

# Start all services
pm2 start ecosystem.config.js

# Enforce HITL on ALL tweets (edit tweet_scheduler.py)
# Set status='hitl_pending' for all pillars temporarily

# Monitor Telegram bot for approvals
pm2 logs vip_hitl_bot
```

### Phase 3: Omnichannel Alpha (Days 15-30)
**Goal**: Full autonomous operation

```bash
# Remove HITL override (only Pillar 12 requires HITL)
# Verify quota enforcement
psql $DATABASE_URL -c "SELECT * FROM twitter_bot.api_quotas WHERE date = CURRENT_DATE"

# Enable Reddit cross-posting
# Verify threads posting
# Monitor analytics
pm2 logs analytics_tracker
```

---

## 🔒 Security Checklist

- [ ] Railway PostgreSQL: IP allowlist configured (Hetzner VPS only)
- [ ] TLS: `sslmode=verify-full` in `DATABASE_URL`
- [ ] Secrets: `.env` encrypted with `sops`, loaded to `/dev/shm/` (tmpfs)
- [ ] Database users: `twitterbot_rw` (RW twitter_bot only), separate from SkinBetAI
- [ ] Telegram: `TELEGRAM_ALLOWED_USER_IDS` hardcoded
- [ ] Auto-patch: 3-layer security gate enabled (Bandit, sandbox, behavioral)
- [ ] Network egress: Docker sandbox with explicit domain allowlist
- [ ] Logs: No secrets in PM2 stdout/stderr (masked tokens)

---

## 📊 Monitoring & Health Checks

### Key Metrics

```bash
# Quota status
psql $DATABASE_URL -c "SELECT date, writes_executed, writes_reserved, hard_capped FROM twitter_bot.api_quotas ORDER BY date DESC LIMIT 7"

# Top performers (RLHF input)
psql $DATABASE_URL -c "SELECT content, engagement_rate, likes, replies FROM twitter_bot.tweets_v2 WHERE rlhf_classified = 'top_5' ORDER BY engagement_rate DESC"

# Shadowban check
psql $DATABASE_URL -c "SELECT test_result, COUNT(*) FROM twitter_bot.canary_logs WHERE timestamp > NOW() - INTERVAL '7 days' GROUP BY test_result"

# Ingestion health
pm2 status
```

### Telegram Commands

```
/start   - Initialize HITL bot
/pending - Show pending approvals
/stats   - Today's quota status
```

### Daily Operator Updates

Daily and urgent private updates are sent through Telegram with `scripts/ops/daily_status_report.py`.
The reporter reads `/dev/shm/.env`, PM2 status, and the existing DB-backed ops dashboard, then sends a sanitized summary without raw logs or secrets.

```bash
# Preview the daily message without sending
cd /home/ubuntu/openclaw
python3 scripts/ops/daily_status_report.py --mode daily --dry-run

# Preview structured status for debugging
python3 scripts/ops/daily_status_report.py --mode daily --dry-run --json

# Send only if critical issues are new or the cooldown has elapsed
python3 scripts/ops/daily_status_report.py --mode alert
```

Recommended cron:

```cron
0 9 * * * cd /home/ubuntu/openclaw && python3 scripts/ops/daily_status_report.py --mode daily >> logs/daily-status-report.log 2>&1
*/15 * * * * cd /home/ubuntu/openclaw && python3 scripts/ops/daily_status_report.py --mode alert >> logs/daily-status-report.log 2>&1
```

Alert de-duplication state is stored in `logs/daily-status-report-state.json`.
Disable urgent alerts by removing the `*/15` cron entry.

### Logs

```bash
# Tail all logs
pm2 logs

# Specific service
pm2 logs tweet_scheduler --lines 100

# Error grep
pm2 logs scrapling_pool --nostream --lines 1000 | grep -i error
```

---

## 🆘 Troubleshooting

### Issue: "403 Forbidden" from X API
**Cause**: Rate limit or account suspension

**Fix**:
```bash
pm2 stop twitter_poster
# Wait 24 hours
# File X Developer appeal if suspended
```

### Issue: Scrapling "Element Not Found"
**Cause**: X/HLTV changed DOM structure

**Fix**: Auto-healing via `scrapling_medic.sh`
```bash
# Check medic logs
tail -f logs/scrapling_medic.log

# Manually trigger Claude Code patch (if auto-patch disabled)
cd /home/ubuntu/openclaw
# Run triage-issue skill manually
```

### Issue: Database connection timeout
**Cause**: Railway PostgreSQL latency or outage

**Fix**:
```bash
# Check Railway status
# Verify connection string
psql $DATABASE_URL -c "SELECT 1"

# Restore from backup if corrupted
gunzip -c backup/db_dump.sql.gz | psql $DATABASE_URL
```

### Issue: HITL Telegram bot not responding
**Cause**: Bot token leaked or invalid

**Fix**:
```bash
# Regenerate bot token via @BotFather on Telegram
# Update .env.enc
sops .env.enc

# Restart bot
pm2 restart vip_hitl_bot
```

---

## 🔄 Weekly Maintenance

### Sunday 23:00 UTC - Auto-RLHF
- `rlhf_tuner.py` runs automatically via cron
- Updates `system_prompt_appendix.txt`
- Enforces drift ceiling (0.70 weekly, 0.55 cumulative)
- Sends Telegram alert if update rejected

### Wednesday 12:00 UTC - Reddit Cross-Post
- `reddit_cross_poster.py` runs via cron
- Takes top thread from past 7 days
- Strips edge signals (odds, bookies, methodology)
- Posts to r/esports

### Sunday 00:00 UTC - A/B Model Evaluation
- `processing/ab_evaluator.py` updates `AB_POOL_AUTO` and `AB_POOL_PREMIUM`
- `OpenRouterClient` hot-reloads the updated pools without a manual restart

### Daily - Database Backup
```bash
# Runs via cron daily 03:15
./scripts/backup_database.sh

# Keep last 14 days
find backup/ -name "twitter_bot_*.dump" -mtime +14 -delete
```

### Quarterly - Entity Refresh
- `scripts/refresh_cs2_entities.py` rebuilds `data/cs2_entities.jsonl` from current constants
- Cron schedule: first day of every third month at 02:00 UTC

---

## 📈 Monthly Costs

| Service | Cost |
|---------|------|
| Hetzner VPS (12GB RAM) | €15-30 |
| Railway PostgreSQL | €5-10 |
| X/Twitter API v2 Basic | $100 |
| OpenRouter (LLM) | ~$25 |
| Mobile Proxies | ~$30 |
| **Total** | **~$175/month** |

---

## 🎓 Architecture Notes

- **No browser-use for posting**: X API v2 only (faster, cheaper, no DOM drift)
- **4G/5G mobile proxies**: Ingestion uses rotating mobile IPs (reads), posting uses datacenter IP (writes) - fully async
- **Pre-commit reservation**: Prevents burst quota violations (writes_reserved + writes_executed < 95)
- **MiroFish veto**: 100-agent swarm for high-risk content (Pillars 3, 7, 12)
- **HITL timeout**: 4 hours, auto-expires to free slot
- **Entangled cross-pollination**: Reddit post updates if Twitter thread has viral sub-debate

---

## 📚 References

- Full Architecture: `TWITTER_BOT_PIPELINE.md`
- Database Schema: `schema.sql`
- PM2 Config: `ecosystem.config.js`
- Python Deps: `requirements.txt`

---

**Built for 21Core AI** | Last Updated: April 13, 2026
