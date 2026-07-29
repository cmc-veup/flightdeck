"""Recover deleted months at FULL per-event fidelity from the agentsview index.

Claude Code deleted April and May 2026 from the main root. But agentsview
(`~/.agentsview/sessions.db`) had already indexed those transcripts — and it
stored, per message, the *raw* `token_usage` JSON alongside the timestamp,
model, sidechain flag and source uuid. That is everything the original JSONL
line carried. So the months aren't lost: they were mirrored into a database
that no cleanup touches.

Unlike `archive.py` (per-session aggregates, low precision), these become real
`usage_events` rows — same granularity as a live transcript, so day/model
breakdowns and the leaderboard export are honest again.

Dedupe: the primary key is (provider, session_id, event_id) and agentsview's
`source_uuid` is the same message uuid flightdeck uses as `event_id`. The
danger is subagent attribution — flightdeck files a subagent event under its
PARENT session id, so if agentsview files it under the subagent's own id the
same event would land twice under different keys. `resolve_session` therefore
walks agentsview's `parent_session_id` before inserting, and `dry_run` exists
so the join can be proven on a month that is already fully ingested (expect
~100% collision) before trusting it on a month that isn't.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .paths import HOME
from .db import open_db

AGENTSVIEW_DB = HOME / ".agentsview" / "sessions.db"


def _account_root(file_path: str | None) -> tuple[str, str]:
    """(provider, account_root) from the indexed transcript path."""
    p = file_path or ""
    if ".claude-accounts/" in p:
        return "claude", p.split(".claude-accounts/", 1)[1].split("/", 1)[0] or "main"
    if ".claude-deepseek" in p:
        return "deepseek", "deepseek"
    if ".claude-ollama" in p:
        return "ollama", "ollama"
    return "claude", "main"


def resolve_session(cur: sqlite3.Cursor, session_id: str, cache: dict) -> str:
    """Walk to the ROOT session so subagent events key the way flightdeck
    files them (under the parent), not under their own transcript id."""
    if session_id in cache:
        return cache[session_id]
    seen, sid = set(), session_id
    while sid and sid not in seen:
        seen.add(sid)
        row = cur.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        parent = row[0] if row and row[0] else None
        if not parent or parent == sid:
            break
        sid = parent
    cache[session_id] = sid or session_id
    return cache[session_id]


def run(db_path=None, source: Path | None = None, months: list[str] | None = None,
        dry_run: bool = False) -> dict:
    """Import agentsview messages as usage_events.

    `months` filters to e.g. ['2026-04','2026-05']; None imports everything.
    `dry_run` reports what WOULD be new without writing — use it to prove the
    key alignment on an already-ingested month.
    """
    src = source or AGENTSVIEW_DB
    if not src.exists():
        return {"found": False}

    av = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    cur = av.cursor()
    conn = open_db(db_path)

    existing = {
        (p, s, e) for p, s, e in
        conn.execute("SELECT provider, session_id, event_id FROM usage_events")
    }
    fd_sessions = {s for (s,) in conn.execute("SELECT DISTINCT session_id FROM usage_events")}

    where = "m.token_usage != '' AND m.timestamp != ''"
    params: list = []
    if months:
        where += " AND substr(m.timestamp,1,7) IN (%s)" % ",".join("?" * len(months))
        params = list(months)

    # Separate cursors: resolve_session() runs queries DURING this iteration,
    # and re-executing on the same cursor silently truncates the scan to its
    # first row (found the hard way — a 43k-row month reported 1).
    scan = av.cursor()
    rows = scan.execute(
        f"""SELECT m.session_id, m.source_uuid, m.timestamp, m.model,
                   m.is_sidechain, m.token_usage, s.file_path, m.ordinal
            FROM messages m LEFT JOIN sessions s ON s.id = m.session_id
            WHERE {where}""",
        params,
    )

    cache: dict = {}
    new, dup, no_uuid, tokens = 0, 0, 0, 0
    batch = []
    for sid, uuid, ts, model, sidechain, usage_json, file_path, ordinal in rows:
        try:
            u = json.loads(usage_json)
        except (TypeError, ValueError):
            continue
        provider, account_root = _account_root(file_path)
        root_session = resolve_session(cur, sid, cache)
        if not uuid:
            # agentsview's early rows (all of April 2026) predate it recording
            # source uuids, so there is no key to dedupe against a transcript.
            # Safe only when flightdeck holds NO event for that session at all:
            # then nothing can collide, and a synthetic ordinal key is stable.
            if root_session in fd_sessions:
                no_uuid += 1
                continue
            uuid = f"av-ord:{ordinal}"
        key = (provider, root_session, uuid)
        if key in existing:
            dup += 1
            continue
        existing.add(key)
        cc = u.get("cache_creation") or {}
        vals = (
            provider, account_root, root_session, uuid, model or None,
            ts if ts.endswith("Z") else ts,
            u.get("input_tokens", 0) or 0,
            u.get("output_tokens", 0) or 0,
            u.get("cache_creation_input_tokens", 0) or 0,
            u.get("cache_read_input_tokens", 0) or 0,
            cc.get("ephemeral_5m_input_tokens", 0) or 0,
            cc.get("ephemeral_1h_input_tokens", 0) or 0,
            0,
            1 if sidechain else 0,
            None, None, None,
        )
        tokens += sum(vals[6:10])
        new += 1
        if not dry_run:
            batch.append(vals)
            if len(batch) >= 5000:
                _flush(conn, batch)
                batch = []
    if not dry_run and batch:
        _flush(conn, batch)
    if not dry_run:
        conn.commit()
    av.close()
    return {"found": True, "new_events": new, "already_present": dup,
            "skipped_no_uuid": no_uuid, "recovered_tokens": tokens,
            "dry_run": dry_run}


def _flush(conn, batch) -> None:
    conn.executemany(
        """INSERT OR IGNORE INTO usage_events
           (provider, account_root, session_id, event_id, model, ts,
            input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
            cache_5m_tokens, cache_1h_tokens, reasoning_tokens, is_sidechain,
            agent_id, cwd, cost_micros)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        batch,
    )
