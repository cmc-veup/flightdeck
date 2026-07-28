"""Report invariants: main + subagent == total, exactly, and source classes."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightdeck import claude_collector, report
from flightdeck.claude_collector import sidechain_class
from flightdeck.db import open_db, refresh_pricing

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


def usage_line(session, uuid, ts, model="claude-fable-5", inp=10, out=5, cc=100,
               cr=200, sidechain=False):
    return json.dumps({
        "type": "assistant", "sessionId": session, "uuid": uuid,
        "timestamp": ts, "isSidechain": sidechain,
        "message": {"model": model, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
        }},
    })


def ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SidechainClassTests(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(sidechain_class(Path("p/sess.jsonl")), 0)
        self.assertEqual(sidechain_class(Path("p/sess/subagents/agent-a.jsonl")), 1)
        self.assertEqual(
            sidechain_class(Path("p/sess/subagents/workflows/wf_1/agent-a.jsonl")), 2
        )


class ReportSplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "usage.db"
        conn = open_db(self.db_path)
        refresh_pricing(conn)
        # build rows via the parser so the test exercises the production path
        proj = Path(self.tmp.name) / "projects" / "-x"
        wf = proj / "s1" / "subagents" / "workflows" / "wf_1"
        sub = proj / "s1" / "subagents"
        wf.mkdir(parents=True)
        (proj / "s1.jsonl").write_text(
            usage_line("s1", "m1", ts(1), model="claude-opus-5", inp=100, out=50)
            + "\n" + usage_line("s1", "m2", ts(2), inp=10, out=5) + "\n"
        )
        (sub / "agent-p.jsonl").write_text(
            usage_line("s1", "sa1", ts(3), sidechain=True, inp=7, out=3, cc=11, cr=13) + "\n"
        )
        (wf / "agent-w.jsonl").write_text(
            usage_line("s1", "wf1", ts(4), model="claude-haiku-4-5", sidechain=True,
                       inp=20, out=9, cc=30, cr=40) + "\n"
        )
        # an event OUTSIDE the 24h window must not appear anywhere
        (proj / "s2.jsonl").write_text(usage_line("s2", "old1", ts(30)) + "\n")
        for f in claude_collector.discover_files(proj.parent):
            claude_collector.insert_rows(conn, "claude", "main", claude_collector.parse_file(f))
        conn.commit()
        conn.close()
        self.data = report.gather(
            since="24h",
            now=NOW.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            db_path=self.db_path,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_main_plus_subagent_equals_total_exactly(self):
        t = self.data["totals"]
        s = self.data["sources"]
        for col in report.TOKEN_COLS + ["events"]:
            self.assertEqual(
                s["main"][col] + s["subagent_total"][col], t[col], f"column {col}"
            )
        self.assertEqual(
            s["main"]["total_tokens"] + s["subagent_total"]["total_tokens"],
            t["total_tokens"],
        )
        self.assertAlmostEqual(
            s["main"]["cost_usd"] + s["subagent_total"]["cost_usd"],
            t["cost_usd"], places=9,
        )

    def test_subagent_total_is_plain_plus_workflow(self):
        s = self.data["sources"]
        for col in report.TOKEN_COLS + ["events"]:
            self.assertEqual(
                s["subagent_plain"][col] + s["workflow_subagent"][col],
                s["subagent_total"][col],
            )

    def test_source_classification_counts(self):
        s = self.data["sources"]
        self.assertEqual(s["main"]["events"], 2)
        self.assertEqual(s["subagent_plain"]["events"], 1)
        self.assertEqual(s["workflow_subagent"]["events"], 1)

    def test_spend_architecture_and_model_source_sections(self):
        sa = self.data["spend_architecture"]
        self.assertEqual(sa["claude/main"]["events"], 2)
        self.assertEqual(sa["claude/subagent"]["events"], 1)
        self.assertEqual(sa["claude/workflow-subagent"]["events"], 1)
        ms = self.data["by_model_source"]
        self.assertIn("claude-opus-5/main", ms)
        self.assertIn("claude-fable-5/main", ms)
        self.assertIn("claude-fable-5/subagent", ms)
        self.assertIn("claude-haiku-4-5/workflow-subagent", ms)
        # section sums also reconcile with the grand total
        for section in (sa, ms):
            self.assertEqual(
                sum(v["events"] for v in section.values()), self.data["totals"]["events"]
            )

    def test_window_excludes_old_events(self):
        self.assertEqual(self.data["totals"]["events"], 4)

    def test_legacy_subagent_key_stable(self):
        legacy = self.data["subagent"]
        self.assertEqual(
            set(legacy.keys()), {"tokens", "share_of_total", "cost_usd"}
        )
        self.assertEqual(
            legacy["tokens"], self.data["sources"]["subagent_total"]["total_tokens"]
        )


if __name__ == "__main__":
    unittest.main()
