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
      max_memory_restart: '800M',
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
      instances: 1,              // Single instance — dedup sets are in-memory
      exec_mode: 'fork',         // MUST be 'fork' for Python (not cluster)
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
      restart_delay: 10000,      // Delay to avoid spamming target servers
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
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
      max_memory_restart: '300M',
      restart_delay: 5000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1',
        ENABLE_MIROFISH_GUARD: 'false'   // Disabled at 53 followers — saves LLM spend
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
      max_memory_restart: '200M',
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
      name: 'scrapling_medic',
      script: 'scripts/utils/scrapling_medic.sh',
      interpreter: 'bash',
      instances: 1,
      autorestart: true,
      watch: false,              // Disabled: watch:true caused restart loop from own log output
      time: true,
      error_file: './logs/medic-error.log',
      out_file: './logs/medic-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z'
    },
    {
      name: 'twitter_poster',
      script: 'scripts/output/twitter_poster.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '200M',
      restart_delay: 5000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
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
      max_memory_restart: '150M',
      restart_delay: 5000,
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
      max_memory_restart: '150M',
      restart_delay: 5000,
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
      name: 'prediction_webhook',
      script: 'scripts/ingestion/prediction_webhook.py',
      interpreter: 'python3',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '150M',
      restart_delay: 5000,
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1'
      },
      error_file: './logs/prediction-webhook-error.log',
      out_file: './logs/prediction-webhook-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    }
  ]
};
