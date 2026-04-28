// PM2 Ecosystem Configuration for Twitter Bot Pipeline V2
// Memory Budget: 12GB total allocated to Twitter Bot
// PostgreSQL hosted on Railway (no local memory footprint)

module.exports = {
  apps: [
    {
      name: 'siftly_ingestor',
      script: 'scripts/ingestion/siftly_engine.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '2000M',
      restart_delay: 5000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1',
        SIFTLY_MODEL_PATH: './models/siftly_v2.pt'
      },
      error_file: './logs/siftly-error.log',
      out_file: './logs/siftly-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'scrapling_pool',
      script: 'scripts/ingestion/ingestion_runner.py',
      interpreter: 'python3',
      instances: 1,              // Single instance — dedup via source_url unique index
      exec_mode: 'fork',         // MUST be 'fork' for Python (not cluster)
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
      restart_delay: 10000,      // Delay to avoid spamming target servers
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1',
        DASHBOARD_REVIEW_ONLY: 'true'
      },
      error_file: './logs/scrapling-error.log',
      out_file: './logs/scrapling-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'tweet_scheduler',
      script: 'scripts/output/tweet_scheduler.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '1500M',
      restart_delay: 5000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1',
        EPISODIC_MEMORY_ENABLED: 'true',
        SENTENCE_TRANSFORMERS_MODEL: 'all-MiniLM-L6-v2',
        SENTENCE_TRANSFORMERS_LOCAL_ONLY: 'true',
        HF_HUB_OFFLINE: '1',
        TRANSFORMERS_OFFLINE: '1',
        DASHBOARD_REVIEW_ONLY: 'true'
      },
      error_file: './logs/scheduler-error.log',
      out_file: './logs/scheduler-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'vip_hitl_bot',
      script: 'scripts/output/vip_hitl_telegram.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '1500M',
      restart_delay: 5000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
      },
      error_file: './logs/telegram-error.log',
      out_file: './logs/telegram-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'twitter_poster',
      script: 'scripts/output/twitter_poster.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
      restart_delay: 5000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1',
        DRY_RUN_MODE: 'false',
        DASHBOARD_REVIEW_ONLY: 'true'
      },
      error_file: './logs/poster-error.log',
      out_file: './logs/poster-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'engagement_tracker',
      script: 'scripts/output/engagement_tracker.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '200M',
      restart_delay: 30000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
      },
      error_file: './logs/engagement-error.log',
      out_file: './logs/engagement-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'follower_growth',
      script: 'scripts/output/follower_growth_tracker.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '100M',
      restart_delay: 60000,      // 60s delay — growth metrics are low-priority
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
      },
      error_file: './logs/follower-growth-error.log',
      out_file: './logs/follower-growth-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'style_scraper',
      script: 'scripts/ingestion/style_scraper.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '300M',
      restart_delay: 30000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
      },
      error_file: './logs/style-scraper-error.log',
      out_file: './logs/style-scraper-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'clip_hunter',
      script: 'scripts/ingestion/clip_hunter.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '400M',
      restart_delay: 30000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
      },
      error_file: './logs/clip-hunter-error.log',
      out_file: './logs/clip-hunter-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'engagement_engine',
      script: 'scripts/output/engagement_engine.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '1G',
      restart_delay: 10000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
      },
      error_file: './logs/engagement-engine-error.log',
      out_file: './logs/engagement-engine-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'tweet_pruner',
      script: 'scripts/output/tweet_pruner.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '100M',
      restart_delay: 60000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
      },
      error_file: './logs/pruner-error.log',
      out_file: './logs/pruner-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'community_liker',
      script: 'scripts/output/community_liker.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '150M',
      restart_delay: 30000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
      },
      error_file: './logs/community-liker-error.log',
      out_file: './logs/community-liker-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'live_watcher',
      script: 'scripts/services/live_match_watcher.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '800M',   // Playwright browser + vision payload
      restart_delay: 15000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1',
        PYTHONPATH: './scripts:./scripts/processing'
      },
      error_file: './logs/live-watcher-error.log',
      out_file: './logs/live-watcher-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'ml_retrain',
      script: 'scripts/processing/ml_retrain.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: false,           // One-shot: runs, exits, waits for next cron trigger
      watch: false,
      cron_restart: '0 23 * * 0',   // Every Sunday at 23:00 UTC
      max_memory_restart: '1500M',
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1',
        PYTHONPATH: './scripts:./scripts/processing'
      },
      error_file: './logs/ml-retrain-error.log',
      out_file: './logs/ml-retrain-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'prediction_webhook',
      script: 'scripts/ingestion/prediction_webhook.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
      restart_delay: 5000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1',
        IN_HOUSE_PREDICTION_FETCH_TIMEOUT: '45',
        IN_HOUSE_PREDICTION_FIXTURE_TIMEOUT: '75',
        IN_HOUSE_PREDICTION_CYCLE_TIMEOUT: '180',
        IN_HOUSE_PREDICTION_PARSER_TIMEOUT: '12'
      },
      error_file: './logs/prediction-webhook-error.log',
      out_file: './logs/prediction-webhook-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'prediction_results',
      script: 'scripts/output/prediction_results.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '150M',
      restart_delay: 10000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
      },
      error_file: './logs/prediction-results-error.log',
      out_file: './logs/prediction-results-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'ab_evaluator',
      script: 'scripts/processing/ab_evaluator.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: false,         // one-shot cron, not a daemon
      watch: false,
      cron_restart: '0 3 * * 0',  // every Sunday at 03:00 UTC
      max_memory_restart: '100M',
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
      },
      error_file: './logs/ab-evaluator-error.log',
      out_file: './logs/ab-evaluator-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    }
  ]
};
