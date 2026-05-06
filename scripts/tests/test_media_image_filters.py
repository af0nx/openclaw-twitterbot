import unittest

from processing.media_manager import MediaManager


class MediaImageFilterTests(unittest.TestCase):
    def test_9ine_query_rejects_wrong_team_bench_image(self):
        query = "9INE bench bnox CS2 esports event photo players stage"
        self.assertFalse(
            MediaManager._url_matches_required_subjects(
                "https://esportsinsider.com/wp-content/uploads/2025/07/G2-benches-Snax.jpg",
                query,
            )
        )
        self.assertTrue(
            MediaManager._url_matches_required_subjects(
                "https://assets.gamearena.gg/wp-content/uploads/2024/05/22151055/9INE-CS.jpg",
                query,
            )
        )

    def test_aether_query_rejects_generic_roster_images(self):
        query = "Aether release roster following internal issues and roster instability CS2 esports event photo players stage"
        self.assertFalse(
            MediaManager._url_matches_required_subjects(
                "https://www.esports.net/wp-content/uploads/2025/10/furia-thunderpick-cs-world-champ-1024x576.jpg",
                query,
            )
        )
        self.assertFalse(
            MediaManager._url_matches_required_subjects(
                "https://www.talkesport.com/wp-content/uploads/FUT-Esports-Reveals-CS2-Roster-scaled.jpeg",
                query,
            )
        )

    def test_esl_pro_league_season_25_rejects_old_seasons(self):
        query = "Saudi Arabia to host ESL Pro League Season 25 CS2 esports event photo players stage"
        self.assertTrue(
            MediaManager._has_conflicting_season(
                "https://pro.eslgaming.com/tour/wp-content/uploads/2026/01/EPL-S23-Invited-Teams-1536x864.png",
                query,
            )
        )
        self.assertTrue(
            MediaManager._has_conflicting_season(
                "https://dmarket.com/blog/cs2-esports-calendar-2025/ESL%20Pro%20League%20Season%2022_hu.webp",
                query,
            )
        )
        self.assertFalse(
            MediaManager._has_conflicting_season(
                "https://example.com/esl-pro-league-season-25-stage.jpg",
                query,
            )
        )

    def test_primary_subject_is_required_when_headline_has_trailing_event_context(self):
        query = "Fisher win PlayVS College League Finals ahead of PGL Astana CS2 esports event photo players stage"
        self.assertFalse(
            MediaManager._url_matches_required_subjects(
                "https://example.com/pgl-astana-betboom-trophy-photo.jpg",
                query,
            )
        )
        self.assertTrue(
            MediaManager._url_matches_required_subjects(
                "https://example.com/fisher-playvs-college-league-finals.jpg",
                query,
            )
        )


if __name__ == "__main__":
    unittest.main()
