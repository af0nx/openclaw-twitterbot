# Security Audit TODO — April 17, 2026

> Full audit of OpenClaw + Twitter Bot Pipeline.  
> Items marked [DONE] have been fixed in this session.  
> Items marked [MANUAL] require human action (credential rotation, legal review).

---

## P0 — CRITICAL (Do today)

- [DONE] **SEC-01** Remove hardcoded DATABASE_URL from `scripts/processing/ab_evaluator.py:67`
- [DONE] **SEC-02** Remove hardcoded DB password from `/home/ubuntu/setup-railway-db.py` (3 locations)
- [DONE] **SEC-03** Extract hardcoded Twitter GQL bearer from `twitter_monitor.py`, `style_scraper.py`, `community_liker.py`, `engagement_tracker.py` → env var
- [DONE] **SEC-04** Fix `.gitignore` to exclude `.env.*`, `config/.env.*` (except `.env.enc`)
- [DONE] **SEC-05** Delete leaked password filename from `~/` (`mCbbtxIXxFumxgCAoXmNaTdFFzTV"...`)
- [DONE] **SEC-06** Delete stale debug files (`db-out.txt`, `railway-setup-output.txt`, `sets}')`)
- [DONE] **SEC-07** Restrict PM2 dump file permissions (`chmod 600 ~/.pm2/dump.pm2*`)
- [MANUAL] **SEC-08** Rotate ALL credentials: Railway DB password, X API keys, OpenRouter key, Telegram bot token
- [MANUAL] **SEC-09** Check git history for committed secrets: `git log --all -p -- config/.env.production`

---

## P1 — HIGH (This week)

- [DONE] **SEC-10** `Dockerfile.bot`: Add non-root user, drop privileges
- [DONE] **SEC-11** `Dockerfile.bot`: Pin base image to SHA256 digest
- [DONE] **SEC-12** `Dockerfile.sandbox-browser`: Add VNC password + bind CDP to localhost
- [DONE] **SEC-13** `fly.toml`: Remove `--allow-unconfigured` from process command
- [DONE] **SEC-14** `docker-compose.yml`: Default bind to `127.0.0.1`, require gateway token
- [DONE] **SEC-15** Replace `pickle.load()` with restricted unpickler in `tone_validator.py`, `persona_classifier.py`
- [DONE] **SEC-16** Fix SQL f-string interpolation in `clip_hunter.py:375`
- [DONE] **SEC-17** Fix `sync-skinbethub.sh`: Add exclusions, quote variables
- [DONE] **SEC-18** Fix `setup.sh`: Replace `curl|sudo bash` with package-manager installs
- [DONE] **SEC-19** `docker-compose.yml`: Fail if `OPENCLAW_GATEWAY_TOKEN` is empty

---

## P2 — MEDIUM (This sprint)

- [DONE] **SEC-20** `schema.sql`: Uncomment and add GRANT statements with proper roles
- [DONE] **SEC-21** `schema.sql`: Add CHECK constraint on `pillar` column
- [DONE] **SEC-22** `schema.sql`: Fix quota view off-by-one (`38` → match `DAILY_TWEET_CAP`)
- [DONE] **SEC-23** Pin Python dependencies to version ranges (`>=x,<y`)
- [DONE] **SEC-24** Consolidate `ecosystem.config.js` → single source `.cjs`
- [MANUAL] **SEC-25** MiroFish AGPL license compliance review — get legal sign-off or replace

---

## P3 — LOW (Backlog)

- [ ] **SEC-26** Add SBOM generation to Docker builds
- [ ] **SEC-27** Add Prometheus metrics endpoint
- [ ] **SEC-28** Create ENUM type for `tweets_v2.status` column
- [ ] **SEC-29** Add Row-Level Security policies to DB tables
- [ ] **SEC-30** Add authentication to dashboard API (skinbethub_twitter)
