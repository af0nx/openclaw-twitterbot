import unittest

from output.tweet_scheduler import TweetScheduler
from processing.content_generator import main_feed_quality_issue as scheduler_quality_issue
from processing.tweet_quality import main_feed_quality_issue as poster_quality_issue


class EnterpriseCopyQualityTests(unittest.TestCase):
    def test_result_headline_uses_won_not_bare_win(self):
        scheduler = TweetScheduler.__new__(TweetScheduler)
        tweet = scheduler._compose_enterprise_news_tweet(
            {
                "source": "dust2us",
                "category": "cs2",
                "headline": "Fisher win PlayVS College League Finals ahead of PGL Astana",
            },
            1,
        )

        self.assertIsNotNone(tweet)
        self.assertIn("Fisher won PlayVS College League Finals", tweet)
        self.assertNotIn("Fisher win PlayVS College League Finals", tweet)

    def test_bare_win_result_copy_is_rejected_by_scheduler_and_poster_gates(self):
        bad = "Fisher win PlayVS College League Finals ahead of PGL Astana.\n\nSkinBetHub AI prediction model is tracking the next match."
        self.assertEqual(
            scheduler_quality_issue(bad, pillar=1),
            "main-feed copy is not enterprise/professional",
        )
        self.assertEqual(
            poster_quality_issue(bad, pillar=1),
            "main-feed copy is not enterprise/professional",
        )


if __name__ == "__main__":
    unittest.main()
