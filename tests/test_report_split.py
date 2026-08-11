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
            set(legacy.keys()),
            {"tokens", "share_of_total", "share_of_provider", "cost_usd"},
        )
        self.assertEqual(
            legacy["tokens"], self.data["sources"]["subagent_total"]["total_tokens"]
        )

    def test_window_accepts_arbitrary_hours_and_days(self):
        self.assertEqual(report.parse_window("48h"), timedelta(hours=48))
        self.assertEqual(report.parse_window("14d"), timedelta(days=14))
        self.assertEqual(report.parse_window("24h"), timedelta(hours=24))
        for bad in ("yesterday", "0h", "24", "h", "-3h", "1w"):
            with self.assertRaises(SystemExit):
                report.parse_window(bad)
        # the 30h-old event is invisible at 24h and visible at 48h
        data = report.gather(
            since="48h",
            now=NOW.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            db_path=self.db_path,
        )
        self.assertEqual(data["totals"]["events"], 5)


def _insert(conn, provider, session, event, ts_, inp, out, sidechain=0,
            model="claude-fable-5"):
    conn.execute(
        """INSERT INTO usage_events (provider, account_root, session_id, event_id,
               model, ts, input_tokens, output_tokens, is_sidechain)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (provider, provider, session, event, model, ts_, inp, out, sidechain),
    )


class PerProviderShareTests(unittest.TestCase):
    """The subagent share's denominator is PER-PROVIDER, never all-provider.

    Only providers that record a delegation dimension (any is_sidechain>0
    row) may enter the denominator. codex writes is_sidechain=0 for every
    row, so a codex-heavy window must leave the subagent ratio untouched —
    the dilution regression this guards against read 3.0% instead of 4.3%
    on 2026-08-07 and looked like a collection failure.
    """

    NOW_Z = NOW.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _gather(self, with_codex: bool) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "usage.db"
        conn = open_db(db_path)
        refresh_pricing(conn)
        _insert(conn, "claude", "s1", "m1", ts(1), inp=100, out=50)
        _insert(conn, "claude", "s1", "sa1", ts(2), inp=30, out=20, sidechain=1)
        if with_codex:
            _insert(conn, "codex", "c1", "r1", ts(3), inp=500, out=300,
                    model="gpt-5.6")
        conn.commit()
        conn.close()
        return report.gather(since="24h", now=self.NOW_Z, db_path=db_path)

    def test_share_denominator_is_per_provider(self):
        # claude: 200 billed tokens, 50 of them subagent -> 25% within claude,
        # regardless of how many codex tokens share the window.
        with_codex = self._gather(with_codex=True)
        without = self._gather(with_codex=False)
        self.assertEqual(
            with_codex["sources"]["subagent_total"]["token_share"], 0.25)
        self.assertEqual(
            without["sources"]["subagent_total"]["token_share"], 0.25)
        self.assertEqual(
            with_codex["sources"]["subagent_total"]["token_share"],
            without["sources"]["subagent_total"]["token_share"],
            "codex tokens inflated the subagent-share denominator",
        )

    def test_all_token_share_is_separate_and_labelled(self):
        data = self._gather(with_codex=True)
        sub = data["sources"]["subagent_total"]
        self.assertEqual(sub["token_share_all"], 0.05)   # 50 / 1,000
        legacy = data["subagent"]
        self.assertEqual(legacy["share_of_total"], 0.05)
        self.assertEqual(legacy["share_of_provider"], 0.25)

    def test_delegation_section_names_both_provider_classes(self):
        data = self._gather(with_codex=True)
        dele = data["delegation"]
        self.assertEqual(dele["providers"], ["claude"])
        self.assertEqual(dele["no_dimension_providers"], ["codex"])
        self.assertEqual(dele["total_tokens"], 200)
        self.assertEqual(dele["no_dimension_tokens"], 800)

    def test_totals_still_span_every_provider(self):
        data = self._gather(with_codex=True)
        self.assertEqual(data["totals"]["total_tokens"], 1000)
        # main + subagent + no-dimension == total, exactly
        self.assertEqual(
            data["sources"]["main"]["total_tokens"]
            + data["sources"]["subagent_total"]["total_tokens"]
            + data["delegation"]["no_dimension_tokens"],
            data["totals"]["total_tokens"],
        )

    def test_human_render_labels_the_all_token_share(self):
        text = report.render_human(self._gather(with_codex=True))
        self.assertIn("within claude", text)
        self.assertIn("no delegation dimension (codex)", text)
        self.assertIn("of all tokens, subagent = 5.0%", text)


if __name__ == "__main__":
    unittest.main()
