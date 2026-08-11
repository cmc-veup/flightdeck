"""Chart honesty: providers without a delegation dimension are their own
series — their tokens never enter the main/subagent split or its denominator."""

import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightdeck import badges
from flightdeck.db import open_db, refresh_pricing


def _insert(conn, provider, session, event, ts, inp, out, sidechain=0,
            model="claude-fable-5"):
    conn.execute(
        """INSERT INTO usage_events (provider, account_root, session_id, event_id,
               model, ts, input_tokens, output_tokens, is_sidechain)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (provider, provider, session, event, model, ts, inp, out, sidechain),
    )


class DailySeriesSplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_db(Path(self.tmp.name) / "usage.db")
        refresh_pricing(self.conn)
        _insert(self.conn, "claude", "s1", "e1", "2026-08-07T10:00:00.000Z", 100, 50)
        _insert(self.conn, "claude", "s1", "e2", "2026-08-07T10:05:00.000Z", 30, 20,
                sidechain=1)
        _insert(self.conn, "codex", "c1", "r1", "2026-08-07T10:10:00.000Z", 500, 300,
                model="gpt-5.6")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_dimensionless_provider_is_its_own_column(self):
        self.assertEqual(
            badges.daily_series(self.conn, 30), [("2026-08-07", 150, 50, 800)]
        )

    def test_svg_renders_other_segment_with_legend_note(self):
        svg = badges.svg_chart(badges.daily_series(self.conn, 30))
        self.assertIn("class='o'", svg)
        self.assertIn("no delegation dimension", svg)
        # the split's two classes are still present
        self.assertIn("class='m'", svg)
        self.assertIn("class='s'", svg)

    def test_svg_tolerates_legacy_three_tuples(self):
        svg = badges.svg_chart([("2026-08-07", 150, 50)])
        self.assertNotIn("class='o'", svg)

    def test_subagent_pct_metric_is_within_provider(self):
        m = badges.collect_metrics(self.conn)
        # 50 subagent of 200 claude tokens = 25%, NOT 50 of 1,000 = 5%
        self.assertAlmostEqual(m["subagent_pct"], 25.0)
        self.assertAlmostEqual(m["subagent_pct_alltime"], 25.0)
        self.assertEqual(m["delegating_providers"], ["claude"])


if __name__ == "__main__":
    unittest.main()
