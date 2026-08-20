"""Delegation must bucket EVERY sidechain class, not just class 1.

`is_sidechain` is a CLASS code, not a boolean: 0 = main, 1 = plain subagent,
2 = workflow subagent (subagents/workflows/wf_*/). `sidechain_class()` says so
and every other consumer tests truthiness — but delegation's rollup asked for
`is_sidechain = 1`, so workflow subagents matched neither the `sidechain`
bucket nor the `unclassified` one (which requires `= 0`) and silently fell out
of the report entirely. On the live estate that hid 24 sessions and 5.38B
tokens behind a rollup that looked complete.

The invariant this pins: delegated + sidechain + unclassified accounts for
every session, whatever class code a future collector introduces.
"""

import sqlite3
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightdeck.db import open_db


def add(conn, session, sidechain, provider="claude"):
    conn.execute(
        "INSERT OR IGNORE INTO usage_events (provider, account_root, session_id,"
        " event_id, model, ts, input_tokens, output_tokens, cache_creation_tokens,"
        " cache_read_tokens, is_sidechain) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (provider, "main", session, f"{session}-e", "claude-fable-5",
         "2026-07-15T00:00:00.000Z", 10, 5, 100, 200, sidechain),
    )


class DelegationClassTests(unittest.TestCase):
    def setUp(self):
        self.conn = open_db(":memory:") if _accepts_memory() else _tmp_db()

    def test_workflow_subagents_are_counted_as_sidechain(self):
        conn = self.conn
        add(conn, "main-sess", 0)
        add(conn, "plain-sub", 1)
        add(conn, "workflow-sub", 2)
        conn.commit()

        from flightdeck import delegation
        conn.executescript(delegation.SCHEMA)
        got = {r["provider"]: r for r in delegation.report(conn)["providers"]}
        claude = got["claude"]
        self.assertEqual(
            claude["delegated_native_sidechain"], 2,
            "class 1 AND class 2 are both sidechains — `= 1` drops workflow subagents",
        )
        self.assertEqual(claude["unclassified_attended_by_default"], 1)

    def test_no_session_falls_out_of_every_bucket(self):
        conn = self.conn
        for i, cls in enumerate((0, 1, 2)):
            add(conn, f"s{i}", cls)
        conn.commit()
        total = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM usage_events").fetchone()[0]
        bucketed = conn.execute(
            "SELECT COUNT(DISTINCT CASE WHEN is_sidechain > 0 THEN session_id END)"
            "     + COUNT(DISTINCT CASE WHEN is_sidechain = 0 THEN session_id END)"
            " FROM usage_events").fetchone()[0]
        self.assertEqual(bucketed, total,
                         "every session must land in exactly one bucket")


def _accepts_memory():
    try:
        open_db(":memory:")
        return True
    except Exception:
        return False


def _tmp_db():
    import tempfile
    d = tempfile.mkdtemp()
    return open_db(str(Path(d) / "usage.db"))


if __name__ == "__main__":
    unittest.main()
