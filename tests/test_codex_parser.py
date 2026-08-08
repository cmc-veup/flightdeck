"""Unit tests for the codex rollout parser.

Covers the fork/resume identity collapse: codex exec rollouts replay their
parent's history INCLUDING the parent's session_meta line. Keying rows on the
last session_meta seen collapsed many files onto one (provider, session_id,
event_id) primary key, and INSERT OR REPLACE kept only the last-scanned file's
totals (~75% of codex burn dropped on Aug 5). Identity is the filename UUID:
one rollout file = one run.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightdeck import codex_collector
from flightdeck.db import SCHEMA


def meta_line(session_id, cwd="/work"):
    return json.dumps({
        "type": "session_meta",
        "timestamp": "2026-08-05T10:00:00.000Z",
        "payload": {"id": session_id, "cwd": cwd},
    })


def token_count_line(total_input, cached, output, reasoning=0,
                     ts="2026-08-05T10:05:00.000Z"):
    return json.dumps({
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total_input,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": reasoning,
                },
            },
        },
    })


def write_rollout(root, uuid, lines):
    path = root / f"rollout-2026-08-05T10-00-00-{uuid}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


UUID_A = "019fd090-5b35-7c80-ad16-2519c0f61a35"
UUID_B = "019fd092-9044-7032-8f83-b83d257e1ef0"
PARENT = "019fd000-0000-7000-8000-000000000000"


class CodexParserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _codex_rows(self):
        return self.conn.execute(
            "SELECT session_id, input_tokens, cache_read_tokens, output_tokens"
            " FROM usage_events WHERE provider='codex' ORDER BY session_id"
        ).fetchall()

    def test_forked_rollouts_sharing_parent_meta_do_not_collapse(self):
        # Two forked files: each opens with its OWN meta, then replays the
        # parent's meta. Last-meta-wins keyed both onto PARENT and kept one row.
        fa = write_rollout(self.root, UUID_A, [
            meta_line(UUID_A), meta_line(PARENT),
            token_count_line(1000, 400, 50),
        ])
        fb = write_rollout(self.root, UUID_B, [
            meta_line(UUID_B), meta_line(PARENT),
            token_count_line(2000, 900, 70),
        ])
        for f in (fa, fb):
            row, _ = codex_collector.parse_file(f)
            codex_collector.insert_row(self.conn, row)
        rows = self._codex_rows()
        self.assertEqual([r[0] for r in rows], [UUID_A, UUID_B])
        # input = input - cached; both files' totals survive
        self.assertEqual(rows[0][1:], (600, 400, 50))
        self.assertEqual(rows[1][1:], (1100, 900, 70))

    def test_session_id_is_filename_uuid_even_without_meta(self):
        f = write_rollout(self.root, UUID_A, [token_count_line(500, 0, 10)])
        row, _ = codex_collector.parse_file(f)
        self.assertEqual(row["session_id"], UUID_A)

    def test_cwd_comes_from_files_own_first_meta(self):
        f = write_rollout(self.root, UUID_A, [
            meta_line(UUID_A, cwd="/own"), meta_line(PARENT, cwd="/parent"),
            token_count_line(100, 0, 5),
        ])
        row, _ = codex_collector.parse_file(f)
        self.assertEqual(row["cwd"], "/own")

    def test_last_token_count_wins_cumulative(self):
        f = write_rollout(self.root, UUID_A, [
            meta_line(UUID_A),
            token_count_line(100, 0, 5, ts="2026-08-05T10:01:00.000Z"),
            token_count_line(900, 300, 42, ts="2026-08-05T10:09:00.000Z"),
        ])
        row, _ = codex_collector.parse_file(f)
        self.assertEqual(
            (row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]),
            (600, 300, 42),
        )
        self.assertEqual(row["ts"], "2026-08-05T10:09:00.000Z")

    def test_no_token_count_returns_none(self):
        f = write_rollout(self.root, UUID_A, [meta_line(UUID_A)])
        self.assertEqual(codex_collector.parse_file(f), (None, None))

    def test_unreadable_file_raises_oserror(self):
        # Fail-closed contract: the caller must be able to see the failure and
        # leave the file unmarked in the checkpoint for a retry.
        with self.assertRaises(OSError):
            codex_collector.parse_file(self.root / "rollout-missing.jsonl")


if __name__ == "__main__":
    unittest.main()
