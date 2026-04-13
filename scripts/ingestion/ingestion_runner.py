#!/usr/bin/env python3
"""
Ingestion Runner - Twitter Bot Pipeline V2
Coordinates all ingestion scripts (called by PM2)
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from ingestion.rss_aggregator import RSSAggregator
from ingestion.twitter_monitor import TwitterVIPMonitor
from ingestion.hltv_monitor import HLTVMonitor

# Logging setup  
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def run_all_ingestors():
    """Run all ingestion components in parallel"""
    logger.info("🚀 Starting Ingestion Runner")
    
    # Initialize all ingestors
    rss_agg = RSSAggregator()
    twitter_mon = TwitterVIPMonitor()
    hltv_mon = HLTVMonitor()
    
    # Run them all in parallel
    tasks = [
        asyncio.create_task(rss_agg.run_forever()),
        asyncio.create_task(twitter_mon.run_forever()),
        asyncio.create_task(hltv_mon.run_forever())
    ]
    
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("⏹️  Shutting down all ingestors...")
        rss_agg.cleanup()
        twitter_mon.cleanup()
        hltv_mon.cleanup()


if __name__ == '__main__':
    asyncio.run(run_all_ingestors())
