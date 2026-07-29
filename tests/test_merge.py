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


def test_row_export_redacts_cwd_by_default(tmp_path):
    """The row file is designed to leave the machine; cwd names clients."""
    import json as _json
    from flightdeck import rows
    db = tmp_path / "u.db"
    c = open_db(db)
    c.execute("INSERT INTO usage_events (provider,account_root,session_id,event_id,"
              "cwd,input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens) "
              "VALUES ('claude','main','s','e','/Users/me/vc/BigClient',1,1,0,8)")
    c.commit()
    out = tmp_path / "r.jsonl"
    rows.export_rows(out, db_path=db)
    rec = _json.loads(out.read_text().splitlines()[0])
    assert "cwd" not in rec
    rows.export_rows(out, db_path=db, include_cwd=True)
    assert "BigClient" in out.read_text()


def test_row_roundtrip_dedupes_across_devices(tmp_path):
    from flightdeck import rows
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    shared = ("claude", "main", "s1", "e1", 1, 1, 0, 8)
    _seed(a, [shared])
    _seed(b, [shared, ("claude", "main", "s2", "e2", 0, 0, 0, 100)])
    f = tmp_path / "b.jsonl"
    rows.export_rows(f, db_path=b)
    s = rows.import_rows(f, db_path=a)
    assert s["new_events"] == 1 and s["new_tokens"] == 100
    assert rows.import_rows(f, db_path=a)["new_events"] == 0


def test_local_mirror_is_not_scanned_as_a_device(tmp_path):
    """The archive root holds THIS machine's mirror. Scanning it re-ingests
    local transcripts under a different provider label, which defeats the
    primary key and double-counts. Only devices/<name>/ may be scanned."""
    from flightdeck import paths
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    arch = tmp_path / "transcript-archive"
    mirror = arch / "claude-deepseek" / "projects"        # local mirror
    mirror.mkdir(parents=True)
    (mirror / "s.jsonl").write_text("{}")
    other = arch / "devices" / "laptop" / "claude-main"   # a real other device
    other.mkdir(parents=True)
    (other / "s.jsonl").write_text("{}")
    import os
    os.environ["TRANSCRIPT_ARCHIVE"] = str(arch)
    try:
        labels = {label for _p, _prov, label in paths.discover_claude_roots(tmp_path)}
    finally:
        os.environ.pop("TRANSCRIPT_ARCHIVE", None)
    assert "laptop:claude-main" in labels
    assert not any("claude-deepseek" in l and ":" not in l for l in labels - {"main"})


def test_badges_never_publish_unsustained_concurrency(tmp_path):
    """The 2026-05-23 artifact: 1,000 sessions each with ONE event. A badge
    must count only sustained sessions, or it publishes a number that breaks
    under the first question asked about it."""
    from flightdeck import badges
    db = tmp_path / "u.db"
    c = open_db(db)
    rows = []
    # 50 one-shot sessions in one bucket (artifact) ...
    for i in range(50):
        rows.append(("claude", "main", f"ghost{i}", f"e{i}", 1, 0, 0, 0))
    # ... plus 3 sessions that actually sustained work
    for s in range(3):
        for e in range(6):
            rows.append(("claude", "main", f"real{s}", f"r{s}-{e}", 1, 0, 0, 0))
    c.executemany(
        "INSERT INTO usage_events (provider,account_root,session_id,event_id,"
        "input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens) "
        "VALUES (?,?,?,?,?,?,?,?)", rows)
    c.execute("UPDATE usage_events SET ts='2026-05-23T18:10:00.000Z'")
    c.commit()
    sustained, raw = badges.peak_concurrency(c, "2026-05-23")
    assert raw == 53, "raw count sees the artifact"
    assert sustained == 3, "the badge must report only sustained sessions"
