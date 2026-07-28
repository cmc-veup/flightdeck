"""Export shape must satisfy viberank's /api/submit validation.

Mirrors the checks in sculptdotfun/viberank src/app/api/submit/route.ts and
src/lib/ccusage.ts (normalizeCcData): required totals fields, non-empty daily
array keyed by date, totalTokens >= the four components, and sanitizable
modelBreakdowns (string modelName, finite non-negative numbers).
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightdeck import export_viberank
from flightdeck.db import open_db, refresh_pricing

REQUIRED_TOTALS = [
    "inputTokens", "outputTokens", "cacheCreationTokens",
    "cacheReadTokens", "totalCost", "totalTokens",
]

ROW_SQL = (
    "INSERT INTO usage_events (provider, account_root, session_id, event_id, model,"
    " ts, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
    " cache_5m_tokens, cache_1h_tokens, reasoning_tokens, is_sidechain, agent_id,"
    " cwd, cost_micros) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,NULL,NULL,NULL)"
)


class ViberankExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "usage.db"
        conn = open_db(self.db_path)
        refresh_pricing(conn)
        rows = [
            # two claude accounts, two models, two days, main + subagent
            ("claude", "main", "s1", "e1", "claude-fable-5",
             "2026-07-27T10:00:00.000Z", 100, 50, 1000, 2000, 400, 600, 0),
            ("claude", "veup", "s2", "e2", "claude-opus-5",
             "2026-07-27T11:00:00.000Z", 10, 5, 100, 200, 0, 0, 0),
            ("claude", "main", "s1", "e3", "claude-fable-5",
             "2026-07-28T09:00:00.000Z", 7, 3, 11, 13, 0, 0, 1),
            # non-claude providers must be excluded
            ("codex", "codex", "c1", "rollout-total", "gpt-5.5",
             "2026-07-28T09:30:00.000Z", 999, 999, 0, 999, 0, 0, 0),
            ("grok", "grok", "g1", "1", "grok-4-1-fast-reasoning",
             "2026-07-28T09:40:00.000Z", 500, 500, 0, 0, 0, 0, 0),
        ]
        conn.executemany(ROW_SQL, rows)
        conn.commit()
        conn.close()
        self.payload = export_viberank.build(db_path=self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_required_totals_fields_present_and_numeric(self):
        totals = self.payload["totals"]
        for f in REQUIRED_TOTALS:
            self.assertIn(f, totals)
            self.assertIsNotNone(totals[f])
            self.assertGreaterEqual(totals[f], 0)

    def test_daily_nonempty_and_date_keyed(self):
        daily = self.payload["daily"]
        self.assertIsInstance(daily, list)
        self.assertGreater(len(daily), 0)
        for d in daily:
            self.assertRegex(d["date"], r"^\d{4}-\d{2}-\d{2}$")
            for f in ("inputTokens", "outputTokens", "cacheCreationTokens",
                      "cacheReadTokens", "totalTokens", "totalCost"):
                self.assertIn(f, d)
            # viberank lower-bound check: totalTokens >= sum of components
            self.assertGreaterEqual(
                d["totalTokens"],
                d["inputTokens"] + d["outputTokens"]
                + d["cacheCreationTokens"] + d["cacheReadTokens"] - 1,
            )

    def test_model_breakdowns_sanitizable(self):
        for d in self.payload["daily"]:
            for mb in d["modelBreakdowns"]:
                self.assertIsInstance(mb["modelName"], str)
                self.assertGreater(len(mb["modelName"]), 0)
                for f in ("inputTokens", "outputTokens", "cacheCreationTokens",
                          "cacheReadTokens", "cost"):
                    self.assertGreaterEqual(mb[f], 0)

    def test_claude_only_accounts_merged(self):
        d27 = next(d for d in self.payload["daily"] if d["date"] == "2026-07-27")
        # main + veup merged into one day
        self.assertEqual(d27["inputTokens"], 110)
        self.assertEqual(sorted(d27["modelsUsed"]),
                         ["claude-fable-5", "claude-opus-5"])
        # codex/grok excluded entirely
        all_models = {m for d in self.payload["daily"] for m in d["modelsUsed"]}
        self.assertNotIn("gpt-5.5", all_models)
        self.assertNotIn("grok-4-1-fast-reasoning", all_models)
        d28 = next(d for d in self.payload["daily"] if d["date"] == "2026-07-28")
        self.assertEqual(d28["inputTokens"], 7)  # subagent row included once

    def test_totals_equal_sum_of_daily(self):
        totals = self.payload["totals"]
        for f in ("inputTokens", "outputTokens", "cacheCreationTokens",
                  "cacheReadTokens", "totalTokens"):
            self.assertEqual(totals[f], sum(d[f] for d in self.payload["daily"]))
        self.assertAlmostEqual(
            totals["totalCost"], sum(d["totalCost"] for d in self.payload["daily"]),
            places=6,
        )

    def test_json_serializable(self):
        json.dumps(self.payload)


if __name__ == "__main__":
    unittest.main()
