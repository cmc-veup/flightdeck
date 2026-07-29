"""Unit tests for the Claude JSONL parser edge cases.

Covers every documented prior-tool bug: sidechain attribution, the
subagents/workflows/wf_*/ layout, cross-Mac dedupe by (sessionId, uuid),
journal.jsonl skipping, synthetic-model rows, and the cache TTL split.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightdeck import claude_collector
from flightdeck.db import SCHEMA


def usage_line(session, uuid, model="claude-fable-5", inp=10, out=5, cc=100, cr=200,
               e5m=0, e1h=100, sidechain=False, agent_id=None, ts="2026-07-28T10:00:00.000Z"):
    return json.dumps({
        "type": "assistant",
        "sessionId": session,
        "uuid": uuid,
        "timestamp": ts,
        "isSidechain": sidechain,
        "agentId": agent_id,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cc,
                "cache_read_input_tokens": cr,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": e5m,
                    "ephemeral_1h_input_tokens": e1h,
                },
            },
        },
    })


class ClaudeParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        self.proj = self.root / "-Users-x-repo"
        self.proj.mkdir(parents=True)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _collect_all(self):
        for f in claude_collector.discover_files(self.root):
            rows = claude_collector.parse_file(f)
            claude_collector.insert_rows(self.conn, "claude", "main", rows)
        self.conn.commit()

    def test_main_session_basic_and_cache_split(self):
        f = self.proj / "sess-1.jsonl"
        f.write_text(usage_line("sess-1", "u1", e5m=40, e1h=60) + "\n")
        self._collect_all()
        row = self.conn.execute(
            "SELECT session_id, input_tokens, output_tokens, cache_creation_tokens,"
            " cache_read_tokens, cache_5m_tokens, cache_1h_tokens, is_sidechain"
            " FROM usage_events"
        ).fetchone()
        self.assertEqual(row, ("sess-1", 10, 5, 100, 200, 40, 60, 0))

    def test_sidechain_attributed_to_parent_not_double_counted(self):
        # main session file + subagent file under subagents/
        (self.proj / "sess-2.jsonl").write_text(usage_line("sess-2", "u-main") + "\n")
        sub = self.proj / "sess-2" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-abc.jsonl").write_text(
            usage_line("sess-2", "u-side", sidechain=True, agent_id="abc") + "\n"
        )
        self._collect_all()
        rows = self.conn.execute(
            "SELECT event_id, session_id, is_sidechain, agent_id FROM usage_events"
            " ORDER BY event_id"
        ).fetchall()
        self.assertEqual(rows, [
            ("u-main", "sess-2", 0, None),
            ("u-side", "sess-2", 1, "abc"),
        ])
        # exactly one row per event: no double count possible
        n = self.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        self.assertEqual(n, 2)

    def test_workflow_layout_discovered(self):
        wf = self.proj / "sess-3" / "subagents" / "workflows" / "wf_01"
        wf.mkdir(parents=True)
        (wf / "agent-wf.jsonl").write_text(
            usage_line("sess-3", "u-wf", sidechain=True) + "\n"
        )
        self._collect_all()
        row = self.conn.execute(
            "SELECT session_id, is_sidechain FROM usage_events"
        ).fetchone()
        # workflow subagents carry source class 2 (still truthy = sidechain)
        self.assertEqual(row, ("sess-3", 2))

    def test_sidechain_missing_sessionid_falls_back_to_layout(self):
        sub = self.proj / "sess-4" / "subagents"
        sub.mkdir(parents=True)
        rec = json.loads(usage_line("ignored", "u-nofb", sidechain=True))
        del rec["sessionId"]
        (sub / "agent-nofb.jsonl").write_text(json.dumps(rec) + "\n")
        self._collect_all()
        row = self.conn.execute("SELECT session_id FROM usage_events").fetchone()
        self.assertEqual(row[0], "sess-4")

    def test_cross_mac_dedupe_by_session_and_uuid(self):
        # same sessionId+uuid appearing under two different project dirs (syncthing)
        other = self.root / "-Users-othermac-repo"
        other.mkdir()
        (self.proj / "sess-5.jsonl").write_text(usage_line("sess-5", "u-dup") + "\n")
        (other / "sess-5.jsonl").write_text(usage_line("sess-5", "u-dup") + "\n")
        self._collect_all()
        n = self.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        self.assertEqual(n, 1)

    def test_duplicate_uuid_within_file_deduped(self):
        f = self.proj / "sess-6.jsonl"
        f.write_text(usage_line("sess-6", "u-x") + "\n" + usage_line("sess-6", "u-x") + "\n")
        self._collect_all()
        n = self.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        self.assertEqual(n, 1)

    def test_journal_skipped(self):
        (self.proj / "journal.jsonl").write_text(usage_line("journal", "u-j") + "\n")
        self._collect_all()
        n = self.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        self.assertEqual(n, 0)

    def test_synthetic_model_skipped(self):
        f = self.proj / "sess-7.jsonl"
        f.write_text(usage_line("sess-7", "u-s", model="<synthetic>") + "\n")
        self._collect_all()
        n = self.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        self.assertEqual(n, 0)

    def test_non_assistant_and_malformed_lines_ignored(self):
        f = self.proj / "sess-8.jsonl"
        f.write_text(
            json.dumps({"type": "user", "uuid": "u-u", "sessionId": "sess-8",
                        "message": {"usage": {"input_tokens": 999}}}) + "\n"
            + "not json {{{\n"
            + usage_line("sess-8", "u-ok") + "\n"
        )
        self._collect_all()
        rows = self.conn.execute("SELECT event_id FROM usage_events").fetchall()
        self.assertEqual(rows, [("u-ok",)])


if __name__ == "__main__":
    unittest.main()


def test_discover_finds_every_config_dir(tmp_path, monkeypatch):
    """Root discovery, not a hardcoded list: new account profiles and
    provider-behind-a-Claude-shell dirs must be picked up automatically."""
    from flightdeck import paths

    for rel in (".claude/projects", ".claude-accounts/veup/projects",
                ".claude-mcc22/projects", ".claude-kimi/projects",
                ".claude-deepseek-test/projects"):
        (tmp_path / rel).mkdir(parents=True)
    (tmp_path / ".claude-noprojects").mkdir()

    found = {label: prov for _p, prov, label in paths.discover_claude_roots(tmp_path)}
    assert found == {
        "main": "claude", "veup": "claude", "mcc22": "claude",
        "kimi": "kimi", "deepseek-test": "deepseek",
    }
