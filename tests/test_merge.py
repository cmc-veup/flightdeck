import sqlite3

from flightdeck import merge
from flightdeck.db import open_db


def _seed(path, rows):
    c = open_db(path)
    c.executemany(
        "INSERT OR IGNORE INTO usage_events (provider,account_root,session_id,event_id,"
        "input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens) "
        "VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    return c


def test_merge_dedupes_sessions_present_on_both_devices(tmp_path):
    """Transcripts sync between machines, so the same session shows up on
    both. Merging must count it once — that is the primary key's job."""
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    shared = ("claude", "main", "sess-1", "evt-1", 1, 1, 0, 8)     # on both
    _seed(a, [shared, ("claude", "main", "sess-A", "evt-A", 0, 0, 0, 100)])
    _seed(b, [shared, ("claude", "main", "sess-B", "evt-B", 0, 0, 0, 500)])

    s = merge.run(b, db_path=a)
    assert s["new_events"] == 1          # only sess-B; the shared row collided
    assert s["new_tokens"] == 500
    assert s["total_events"] == 3
    assert s["total_tokens"] == 610      # 10 + 100 + 500, shared counted once


def test_merge_is_idempotent(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _seed(a, [("claude", "main", "s1", "e1", 1, 0, 0, 0)])
    _seed(b, [("claude", "main", "s2", "e2", 5, 0, 0, 0)])
    first = merge.run(b, db_path=a)
    second = merge.run(b, db_path=a)
    assert first["new_events"] == 1
    assert second["new_events"] == 0     # re-running changes nothing
