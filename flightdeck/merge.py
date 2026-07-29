"""Merge another device's flightdeck database into this one.

The multi-device problem: each machine reads only its own transcripts, so
each has a partial picture. Three ways to get one number out of several
devices, in ascending order of how much you have to trust the network:

  1. `flightdeck merge <other.db>` — run `collect` on each device, copy the
     small SQLite file over, merge. This module. Nothing but usage rows moves,
     so no prompts or code leave any machine.
  2. `scripts/grab-device.sh <host> <label>` — rsync another machine's raw
     transcripts into `<archive>/<label>/`, which discovery then ingests
     locally. Use when you want the transcripts themselves preserved too.
  3. Sync the transcript directories with Syncthing and collect on one box.

What you must NOT do is share one `usage.db` over Dropbox/Syncthing and write
to it from several machines: SQLite over a file-sync layer corrupts.

Dedupe is structural, not heuristic. Rows are keyed
`(provider, session_id, event_id)`, so a session that exists on two devices —
the common case, since transcripts sync — merges to one copy. Merging the
same database twice is a no-op, which makes this safe to run on a schedule.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import open_db

USAGE_COLS = (
    "provider, account_root, session_id, event_id, model, ts, "
    "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, "
    "cache_5m_tokens, cache_1h_tokens, reasoning_tokens, is_sidechain, "
    "agent_id, cwd, cost_micros"
)

ARCHIVE_COLS = (
    "source_file, origin, machine, provider, account_root, project, model, "
    "day, is_sidechain, input_tokens, output_tokens, cache_creation_tokens, "
    "cache_read_tokens, messages, still_on_disk"
)


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def run(other: str | Path, db_path=None, device: str | None = None) -> dict:
    """Merge `other` (another machine's usage.db) into the local database.

    `device` re-labels the incoming account roots as `<device>:<root>` so a
    report can still separate machines. Without it, rows keep their original
    labels and the two devices' totals simply combine.
    """
    src = Path(other).expanduser()
    if not src.exists():
        raise SystemExit(f"no database at {src}")

    conn = open_db(db_path)
    before = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens+output_tokens"
        "+cache_creation_tokens+cache_read_tokens),0) FROM usage_events"
    ).fetchone()

    conn.execute("ATTACH DATABASE ? AS src", (str(src),))
    try:
        root = "? || ':' || src.usage_events.account_root" if device else "src.usage_events.account_root"
        params = (device,) if device else ()
        conn.execute(
            f"""INSERT OR IGNORE INTO usage_events ({USAGE_COLS})
                SELECT provider, {root.replace('src.usage_events.', '')},
                       session_id, event_id, model, ts,
                       input_tokens, output_tokens, cache_creation_tokens,
                       cache_read_tokens, cache_5m_tokens, cache_1h_tokens,
                       reasoning_tokens, is_sidechain, agent_id, cwd, cost_micros
                FROM src.usage_events""",
            params,
        )
        archived = 0
        if _table_exists(conn, "archived_usage"):
            cur = conn.execute("SELECT 1 FROM src.sqlite_master "
                               "WHERE type='table' AND name='archived_usage'")
            if cur.fetchone():
                conn.execute(
                    f"""INSERT OR IGNORE INTO archived_usage ({ARCHIVE_COLS})
                        SELECT {ARCHIVE_COLS} FROM src.archived_usage"""
                )
                archived = conn.total_changes
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE src")

    after = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens+output_tokens"
        "+cache_creation_tokens+cache_read_tokens),0) FROM usage_events"
    ).fetchone()
    return {
        "source": str(src),
        "new_events": after[0] - before[0],
        "new_tokens": after[1] - before[1],
        "total_events": after[0],
        "total_tokens": after[1],
        "archived_rows_touched": archived,
    }
