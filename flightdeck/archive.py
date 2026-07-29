"""Recover usage from transcripts that no longer exist on disk.

Claude Code deletes transcripts after `cleanupPeriodDays` (default 30), and
disk-pressure cleanups take more. That makes the live corpus a *rolling
window*, not a ledger: on this machine April and May 2026 are simply gone
from the main root, and mission-control's own cumulative counter was seen
DROPPING from 56.6B (2026-05-22) to 46.9B (2026-06-12) — impossible unless
source files vanished.

Anything that reports "all-time" by scanning transcripts is therefore
reporting *what survived*, not what was spent. This module reconstructs the
lost portion from per-file aggregates left behind by earlier tools —
today `~/.claude/usage-checkpoint.json` (spend-tracker.py), which still
holds 6,608 file records for files that are all gone, including the mb1
machine (`/Users/mchack/...`).

Reconstructed rows live in their OWN table. They are per-file aggregates,
not per-event rows: mixing them into `usage_events` would fake a precision
they don't have and risk double-counting anything that did survive. Reports
add them as a clearly-labelled archive line.
"""

from __future__ import annotations

import json
from pathlib import Path

from .paths import HOME
from .db import open_db

CHECKPOINT = HOME / ".claude" / "usage-checkpoint.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS archived_usage (
    source_file   TEXT PRIMARY KEY,       -- absolute path of the (now deleted) transcript
    origin        TEXT NOT NULL,          -- which archive it was recovered from
    machine       TEXT,                   -- mb1 (mchack) | local | unknown
    provider      TEXT NOT NULL,
    account_root  TEXT NOT NULL,
    project       TEXT,
    model         TEXT,
    day           TEXT,                   -- YYYY-MM-DD
    is_sidechain  INTEGER NOT NULL DEFAULT 0,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    messages      INTEGER NOT NULL DEFAULT 0,
    still_on_disk INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_archived_day ON archived_usage(day);
"""


def classify(path: str, rec: dict) -> tuple[str, str, int]:
    """(machine, account_root, is_sidechain) from the recorded path.

    Structural only — path segments and the archive's own `source` field.
    """
    p = Path(path)
    machine = "mb1" if "/Users/mchack/" in path else "local"
    if ".claude-accounts/" in path:
        parts = path.split(".claude-accounts/", 1)[1].split("/", 1)
        account_root = parts[0] or "unknown"
    elif ".claude-deepseek" in path:
        account_root = "deepseek"
    else:
        account_root = "main"
    sidechain = 1 if ("/subagents/" in path or p.name.startswith("agent-")) else 0
    if not sidechain and str(rec.get("source", "")).startswith("subagent"):
        sidechain = 1
    return machine, account_root, sidechain


def import_checkpoint(db_path=None, checkpoint: Path | None = None) -> dict:
    """Load the spend-tracker checkpoint. Returns a summary dict.

    `still_on_disk` is recorded per row so reports can add ONLY the rows whose
    transcripts are gone — the surviving ones are already counted, per event,
    in `usage_events`.
    """
    src = checkpoint or CHECKPOINT
    conn = open_db(db_path)
    conn.executescript(SCHEMA)
    if not src.exists():
        return {"found": False, "rows": 0, "recovered_tokens": 0}

    data = json.loads(src.read_text())
    sessions = data.get("sessions") or data
    rows, recovered, surviving = 0, 0, 0
    for path, rec in sessions.items():
        if not isinstance(rec, dict):
            continue
        machine, account_root, sidechain = classify(path, rec)
        on_disk = 1 if Path(path).exists() else 0
        inp = rec.get("input_tokens") or 0
        out = rec.get("output_tokens") or 0
        cw = rec.get("cache_create_tokens") or rec.get("cache_creation_tokens") or 0
        cr = rec.get("cache_read_tokens") or 0
        conn.execute(
            """INSERT INTO archived_usage
               (source_file, origin, machine, provider, account_root, project, model,
                day, is_sidechain, input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens, messages, still_on_disk)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_file) DO UPDATE SET
                 still_on_disk=excluded.still_on_disk""",
            (path, "usage-checkpoint", machine,
             "deepseek" if account_root == "deepseek" else "claude",
             account_root, rec.get("project"), rec.get("model"),
             (rec.get("date") or "")[:10], sidechain,
             inp, out, cw, cr, rec.get("messages") or 0, on_disk),
        )
        rows += 1
        total = inp + out + cw + cr
        if on_disk:
            surviving += total
        else:
            recovered += total
    conn.commit()
    return {
        "found": True,
        "rows": rows,
        "recovered_tokens": recovered,
        "surviving_tokens": surviving,
    }


def archive_totals(conn) -> dict:
    """Tokens the archive contributes that NOTHING else has.

    An archive row is superseded once its session exists as per-event rows
    (e.g. recovered from the agentsview index). Counting both would inflate
    the total twice over: once by duplication, and once because the archive's
    aggregates are themselves overstated — for the 70,743 sessions where both
    sources exist, mission-control's numbers run **1.41x** the per-event
    truth (its rglob counted subagent transcripts as sessions AND inside
    their parents). Per-event always wins; the archive only fills gaps.
    """
    try:
        live = {s[:11] for (s,) in
                conn.execute("SELECT DISTINCT session_id FROM usage_events")}
        rows = conn.execute(
            """SELECT source_file, day, input_tokens+output_tokens
                      +cache_creation_tokens+cache_read_tokens
               FROM archived_usage WHERE still_on_disk = 0"""
        ).fetchall()
    except Exception:
        return {"files": 0, "tokens": 0, "first_day": None, "last_day": None,
                "superseded_files": 0, "superseded_tokens": 0}

    n = tok = sup_n = sup_tok = 0
    days = []
    for source_file, day, t in rows:
        sid = (source_file.split("mc-cache:", 1)[1] if source_file.startswith("mc-cache:")
               else Path(source_file).stem)[:11]
        if sid in live:
            sup_n += 1
            sup_tok += t or 0
            continue
        n += 1
        tok += t or 0
        if day:
            days.append(day)
    return {"files": n, "tokens": tok,
            "first_day": min(days) if days else None,
            "last_day": max(days) if days else None,
            "superseded_files": sup_n, "superseded_tokens": sup_tok}


MC_CACHE = Path("/Users/christianmc/vc/.usage-cache.json")


def import_mission_control_cache(db_path=None, cache: Path | None = None) -> dict:
    """Recover per-session aggregates from mission-control's disk cache.

    `~/vc/.usage-cache.json` (scanned 2026-06-12) holds 75k per-session rows
    from an era whose transcripts have since been deleted — it is the only
    surviving record of April/May 2026.

    Join key gotcha: mission-control truncates session ids to 11 chars, so a
    naive comparison against full UUIDs shows zero overlap and would re-add
    everything. Dedupe is on the truncated prefix against (a) sessions already
    ingested per-event, (b) the checkpoint archive, (c) transcripts still on
    disk. Only genuinely-vanished sessions are recorded as recovered.
    """
    src = cache or MC_CACHE
    conn = open_db(db_path)
    conn.executescript(SCHEMA)
    if not src.exists():
        return {"found": False, "rows": 0, "recovered_tokens": 0}

    rows = [r for r in json.loads(src.read_text()).get("sessions", []) if isinstance(r, dict)]
    if not rows:
        return {"found": True, "rows": 0, "recovered_tokens": 0}
    width = len(rows[0].get("id") or "") or 11

    live = {s[:width] for (s,) in conn.execute("SELECT DISTINCT session_id FROM usage_events")}
    ck = {Path(p).stem[:width] for (p,) in conn.execute("SELECT source_file FROM archived_usage")}
    on_disk = set()
    projects = HOME / ".claude" / "projects"
    if projects.exists():
        on_disk = {p.stem[:width] for p in projects.rglob("*.jsonl")}
    already = live | ck | on_disk

    seen, recovered, skipped = set(), 0, 0
    for r in rows:
        sid = r.get("id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        if sid in already:
            skipped += 1
            continue
        inp = r.get("input_tokens") or 0
        out = r.get("output_tokens") or 0
        cw = r.get("cache_creation_tokens") or 0
        cr = r.get("cache_read_tokens") or 0
        cwd = r.get("cwd") or ""
        conn.execute(
            """INSERT INTO archived_usage
               (source_file, origin, machine, provider, account_root, project, model,
                day, is_sidechain, input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens, messages, still_on_disk)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
               ON CONFLICT(source_file) DO NOTHING""",
            (f"mc-cache:{sid}", "mission-control-cache",
             "mb1" if "/Users/mchack/" in cwd else "local", "claude",
             (r.get("account") or "main").lower(), r.get("project"), r.get("model"),
             (r.get("first_ts") or r.get("last_ts") or "")[:10],
             1 if str(r.get("source", "")).startswith("agent") else 0,
             inp, out, cw, cr, r.get("messages") or 0),
        )
        recovered += inp + out + cw + cr
    conn.commit()
    return {"found": True, "rows": len(seen), "skipped_already_counted": skipped,
            "recovered_tokens": recovered}
