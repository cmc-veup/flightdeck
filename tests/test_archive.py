import json

from flightdeck import archive


def test_classify_identifies_machine_account_and_sidechain():
    m, root, side = archive.classify(
        "/Users/mchack/.claude/projects/-p/s.jsonl", {})
    assert (m, root, side) == ("mb1", "main", 0)
    m, root, side = archive.classify(
        "/Users/x/.claude/projects/-p/sess/subagents/agent-a1.jsonl", {})
    assert (m, root, side) == ("local", "main", 1)
    _m, root, _s = archive.classify(
        "/Users/x/.claude-accounts/veup/projects/-p/s.jsonl", {})
    assert root == "veup"


def test_import_counts_only_deleted_transcripts_as_recovered(tmp_path, monkeypatch):
    """A surviving transcript is already counted per-event in usage_events —
    only vanished ones may be added, or the archive double-counts."""
    live = tmp_path / "live.jsonl"
    live.write_text("{}")
    ckpt = tmp_path / "usage-checkpoint.json"
    ckpt.write_text(json.dumps({"sessions": {
        str(live): {"input_tokens": 1, "output_tokens": 2,
                    "cache_read_tokens": 7, "cache_create_tokens": 0,
                    "date": "2026-03-01"},
        "/gone/deleted.jsonl": {"input_tokens": 10, "output_tokens": 20,
                                "cache_read_tokens": 70, "cache_create_tokens": 0,
                                "date": "2026-03-02"},
    }}))
    db = tmp_path / "usage.db"
    s = archive.import_checkpoint(db_path=db, checkpoint=ckpt)
    assert s["rows"] == 2
    assert s["recovered_tokens"] == 100
    assert s["surviving_tokens"] == 10

    from flightdeck.db import open_db
    assert archive.archive_totals(open_db(db))["tokens"] == 100


def test_mc_cache_dedupes_on_truncated_id(tmp_path):
    """mission-control truncates session ids; a full-UUID comparison would
    show zero overlap and re-import everything already counted."""
    import sqlite3
    from flightdeck import archive as A
    from flightdeck.db import open_db
    db = tmp_path / "u.db"
    conn = open_db(db)
    conn.executescript(A.SCHEMA)
    conn.execute(
        "INSERT INTO usage_events (provider,account_root,session_id,event_id,"
        "input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens) "
        "VALUES ('claude','main','abcdefgh-12-3456-7890-aaaaaaaaaaaa','e1',1,1,0,0)")
    conn.commit()
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"sessions": [
        {"id": "abcdefgh-12", "input_tokens": 5, "output_tokens": 5,
         "cache_read_tokens": 0, "cache_creation_tokens": 0, "first_ts": "2026-05-01"},
        {"id": "zzzzzzzz-99", "input_tokens": 100, "output_tokens": 0,
         "cache_read_tokens": 0, "cache_creation_tokens": 0, "first_ts": "2026-05-02"},
    ]}))
    s = A.import_mission_control_cache(db_path=db, cache=cache)
    assert s["skipped_already_counted"] == 1
    assert s["recovered_tokens"] == 100


def test_agentsview_recovery_keys_and_no_uuid_safety(tmp_path):
    """Recovery must key subagent events under the PARENT session (so they
    collide with transcript-derived rows), and may only synthesize a key for
    uuid-less rows when flightdeck holds nothing for that session."""
    import sqlite3
    from flightdeck import agentsview_import as A
    from flightdeck.db import open_db

    av = tmp_path / "av.db"
    c = sqlite3.connect(av)
    c.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, file_path TEXT, parent_session_id TEXT);
        CREATE TABLE messages (session_id TEXT, source_uuid TEXT, timestamp TEXT,
          model TEXT, is_sidechain INT, token_usage TEXT, ordinal INT);
    """)
    c.execute("INSERT INTO sessions VALUES ('child','/p/s.jsonl','parent')")
    c.execute("INSERT INTO sessions VALUES ('parent','/p/s.jsonl',NULL)")
    c.execute("INSERT INTO sessions VALUES ('lonely','/p/x.jsonl',NULL)")
    usage = json.dumps({"input_tokens": 1, "output_tokens": 2,
                        "cache_read_input_tokens": 7, "cache_creation_input_tokens": 0})
    # subagent event, already ingested under the parent → must dedupe
    c.execute("INSERT INTO messages VALUES ('child','u1','2026-05-01T00:00:00Z','m',1,?,0)", (usage,))
    # uuid-less row for a session flightdeck already knows → must be skipped
    c.execute("INSERT INTO messages VALUES ('parent','','2026-05-01T00:00:00Z','m',0,?,5)", (usage,))
    # uuid-less row for a session flightdeck has never seen → safe to recover
    c.execute("INSERT INTO messages VALUES ('lonely','','2026-04-01T00:00:00Z','m',0,?,1)", (usage,))
    c.commit()

    db = tmp_path / "u.db"
    conn = open_db(db)
    conn.execute("INSERT INTO usage_events (provider,account_root,session_id,event_id,"
                 "input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens) "
                 "VALUES ('claude','main','parent','u1',1,2,0,7)")
    conn.commit()

    r = A.run(db_path=db, source=av)
    assert r["already_present"] == 1, "subagent event must key under its parent"
    assert r["skipped_no_uuid"] == 1, "uuid-less row for a known session is unsafe"
    assert r["new_events"] == 1 and r["recovered_tokens"] == 10
