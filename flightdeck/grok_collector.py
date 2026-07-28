"""Grok CLI collector: reads real usage_events rows out of ~/.grok/grok.db.

Grok reports tokens AND cost_micros per event, so its cost is provider-reported
rather than computed from the pricing table. Incremental via a max-rowid cursor
in the checkpoint.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

INSERT_SQL = (
    "INSERT OR IGNORE INTO usage_events (provider, account_root, session_id, event_id,"
    " model, ts, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
    " cache_5m_tokens, cache_1h_tokens, reasoning_tokens, is_sidechain, agent_id, cwd,"
    " cost_micros) VALUES ('grok','grok',?,?,?,?,?,?,0,0,0,0,0,0,NULL,NULL,?)"
)


def collect(conn, grok_db: Path, last_id: int) -> int:
    """Copy new grok usage rows; returns the new cursor (max id seen)."""
    if not grok_db.is_file():
        return last_id
    src = sqlite3.connect(f"file:{grok_db}?mode=ro", uri=True)
    try:
        rows = src.execute(
            "SELECT id, session_id, model, input_tokens, output_tokens,"
            " cost_micros, created_at FROM usage_events WHERE id > ? ORDER BY id",
            (last_id,),
        ).fetchall()
    finally:
        src.close()
    max_id = last_id
    for rid, session_id, model, inp, out, cost_micros, created_at in rows:
        conn.execute(
            INSERT_SQL,
            (
                session_id or "unknown", str(rid), model, created_at,
                int(inp or 0), int(out or 0), cost_micros,
            ),
        )
        max_id = max(max_id, rid)
    return max_id
