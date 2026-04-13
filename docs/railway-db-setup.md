# Railway PostgreSQL 16 — Setup Guide

> Database for **21Core AI Twitter Bot Pipeline V2** + **SkinBetAI signal_map**
> Shared Railway PostgreSQL 16 instance with schema-level isolation.

---

## 1. Create the Railway Project

```bash
# Install Railway CLI
npm i -g @railway/cli

# Login
railway login

# Create project (or link to existing)
railway init        # New project: "21core-data"
# — or —
railway link        # Link to existing project
```

## 2. Provision PostgreSQL 16

Via the **Railway Dashboard** (recommended):

1. Go to [railway.app](https://railway.app) → your project
2. Click **"+ New"** → **Database** → **PostgreSQL**
3. Railway provisions PostgreSQL 16 automatically
4. Copy the **DATABASE_URL** from the **Variables** tab

Or via CLI:
```bash
railway add --plugin postgresql
railway variables   # Shows DATABASE_URL
```

## 3. Enable Required Extensions

Connect to the database and enable extensions:

```bash
# Connect via psql (uses Railway's proxy)
railway connect postgresql

# Or connect directly:
psql "$DATABASE_URL"
```

```sql
-- Required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS vector;        -- pgvector (semantic embeddings)
CREATE EXTENSION IF NOT EXISTS pgaudit;       -- Audit logging (security)

-- Verify
SELECT extname, extversion FROM pg_extension;
```

> **Note**: Railway PostgreSQL 16 includes pgvector out of the box. If `CREATE EXTENSION vector` fails, contact Railway support — it should be available on all plans.

## 4. Run the Schema

```bash
# From repo root
psql "$DATABASE_URL" -f schema.sql
```

This creates the `twitter_bot` schema with all tables:
- `siftly_events` — Media analysis + VECTOR(768) embeddings
- `events` — Multi-source news ingestion
- `tweets_v2` — Generated tweet queue + RLHF analytics
- `api_quotas` — 40/day rate limit tracker with reservation locking
- `monitored_accounts` — VIP account list
- `canary_logs` — Shadowban health monitoring
- `hitl_engaged_7d` — Factor #33 feedback loop guard
- `rlhf_history` — Weekly auto-learning tuning history
- `reddit_posts` — Cross-post tracking
- `system_metrics` — Observability metrics

## 5. Create Database Users

Railway provides a single superuser by default. For production isolation, create dedicated roles:

```sql
-- Read-write user for the Twitter bot daemons
CREATE USER twitterbot_rw WITH PASSWORD 'GENERATE_STRONG_PASSWORD_HERE';
GRANT USAGE ON SCHEMA twitter_bot TO twitterbot_rw;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA twitter_bot TO twitterbot_rw;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA twitter_bot TO twitterbot_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA twitter_bot GRANT ALL ON TABLES TO twitterbot_rw;

-- Read-write user for SkinBetAI (separate schema)
CREATE USER skinbetai_rw WITH PASSWORD 'GENERATE_STRONG_PASSWORD_HERE';
-- SkinBetAI uses its own schema (signal_map), grant accordingly

-- Read-only user for analytics dashboards
CREATE USER analytics_ro WITH PASSWORD 'GENERATE_STRONG_PASSWORD_HERE';
GRANT USAGE ON SCHEMA twitter_bot TO analytics_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA twitter_bot TO analytics_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA twitter_bot GRANT SELECT ON TABLES TO analytics_ro;
```

## 6. Configure Connection on the VPS

Store the connection string in your encrypted env file:

```bash
# On your Hetzner VPS
sudo nano /dev/shm/.env
```

Add:
```env
DATABASE_URL=postgresql://twitterbot_rw:PASSWORD@RAILWAY_HOST:PORT/railway?schema=twitter_bot
RAILWAY_DB_HOST=your-host.railway.app
RAILWAY_DB_PORT=your-port
RAILWAY_DB_NAME=railway
```

Encrypt with sops:
```bash
sops --encrypt --age $(cat ~/.config/sops/age/keys.txt | grep "public" | awk '{print $NF}') \
  /dev/shm/.env > config/env.enc.yaml
```

## 7. Connection Pooling (Optional but Recommended)

Railway supports PgBouncer via the **TCP Proxy**. For the Twitter bot's 5 PM2 daemons, connection pooling prevents exhausting Railway's connection limit:

```env
# Use Railway's internal proxy URL (lower latency)
DATABASE_URL=postgresql://twitterbot_rw:PASSWORD@PROXY_HOST:PROXY_PORT/railway

# Or set pool size in psycopg2
# In Python: psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=10, dsn=DATABASE_URL)
```

## 8. Verify Setup

```bash
# Quick health check
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM twitter_bot.monitored_accounts;"
# Should return the seed VIP count (5+)

# Check pgvector
psql "$DATABASE_URL" -c "SELECT embedding IS NOT NULL AS has_embedding FROM twitter_bot.siftly_events LIMIT 1;"

# Check reservation function
psql "$DATABASE_URL" -c "SELECT twitter_bot.reserve_tweet_slot();"
# Should return TRUE (first slot of the day)
```

## 9. Backups

Railway provides automatic daily backups on paid plans. For additional safety:

```bash
# Manual backup (run weekly via cron)
pg_dump "$DATABASE_URL" \
  --schema=twitter_bot \
  --format=custom \
  --file="backup/twitter_bot_$(date +%Y%m%d).dump"

# Restore
pg_restore --dbname="$DATABASE_URL" --schema=twitter_bot backup/twitter_bot_YYYYMMDD.dump
```

## 10. Monitoring

Railway Dashboard shows:
- CPU / Memory / Disk usage
- Active connections
- Query performance (slow query log)

For programmatic monitoring, the `system_metrics` table tracks pipeline health:
```sql
SELECT metric_name, metric_value, timestamp
FROM twitter_bot.system_metrics
WHERE timestamp > NOW() - INTERVAL '1 hour'
ORDER BY timestamp DESC;
```

---

## Quick Reference

| Item | Value |
|------|-------|
| Engine | PostgreSQL 16 |
| Extensions | uuid-ossp, pgcrypto, vector, pgaudit |
| Schema | `twitter_bot` (isolated from `signal_map`) |
| Embedding dim | VECTOR(768) |
| Rate limit | 40 tweets/day (enforced by `reserve_tweet_slot()`) |
| Users | twitterbot_rw, skinbetai_rw, analytics_ro |
| Backup | Railway auto + weekly pg_dump |
