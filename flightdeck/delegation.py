"""Spawn-manifest ingestion and the mechanical delegated-session join.

Every orchestrator-spawned AI session gets a provenance stamp at spawn time
(bin/fd-spawn-stamp / bin/fd-stamp-exec): an env marker in the child process
plus one JSONL line in ~/.flightdeck/spawn-manifest/YYYY-MM.jsonl:

    {ts, orchestrator, provider,
     session_hint: {cwd, tmux_pane, tmux_session, pid},
     bead_id, operator}

This module is the collector side: it ingests those manifest lines into
usage.db and joins them to sessions MECHANICALLY — recorded fields only,
no inference, no classification heuristics (ZFC):

  * a stamp matches a session when provider is equal, the session's first
    recorded event falls inside a fixed time window after the stamp, and —
    when both sides recorded a cwd — the cwd is equal;
  * each stamp claims at most one session and each session at most one
    stamp; candidates are consumed in timestamp order (deterministic);
  * a session with no matching stamp is NOT guessed at: it stays out of
    session_delegation and reports as "unclassified (attended-by-default)".

Known, accepted gaps (labelled, never fudged):
  * resumed sessions (claude -r) have first events far before any new stamp
    and therefore never match — they stay unclassified;
  * Claude Code sidechains already carry delegation natively via
    is_sidechain > 0 and are reported as their own bucket, not stamped.

Usage:
    python3 -m flightdeck.delegation ingest    # manifest files -> spawn_stamps
    python3 -m flightdeck.delegation annotate  # spawn_stamps -> session_delegation
    python3 -m flightdeck.delegation report    # delegated / sidechain / unclassified
    python3 -m flightdeck.delegation run       # all three
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .paths import DB_PATH, FLIGHTDECK_DIR, ensure_dirs

MANIFEST_DIR = FLIGHTDECK_DIR / "spawn-manifest"

# Fixed, recorded-field join constants. Not tunable per run: changing them
# changes what "delegated" means, so they live here in code review's sight.
WINDOW_BEFORE_S = 5      # clock-rounding guard; stamp is written pre-exec
WINDOW_AFTER_S = 900     # a spawned CLI must produce its first event within 15m

SCHEMA = """
CREATE TABLE IF NOT EXISTS spawn_stamps (
    stamp_id     TEXT PRIMARY KEY,   -- sha256 of the raw manifest line
    ts           TEXT NOT NULL,
    orchestrator TEXT NOT NULL,
    provider     TEXT,
    cwd          TEXT,
    tmux_pane    TEXT,
    tmux_session TEXT,
    pid          INTEGER,
    bead_id      TEXT,
    operator     TEXT,
    raw          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_spawn_stamps_ts ON spawn_stamps(ts);

CREATE TABLE IF NOT EXISTS session_delegation (
    provider   TEXT NOT NULL,
    session_id TEXT NOT NULL,
    stamp_id   TEXT NOT NULL REFERENCES spawn_stamps(stamp_id),
    matched_by TEXT NOT NULL,   -- the literal recorded-field rule that matched
    PRIMARY KEY (provider, session_id)
) WITHOUT ROWID;
"""


def _connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def ingest(conn: sqlite3.Connection, manifest_dir: Path | None = None) -> int:
    """Read every spawn-manifest JSONL file; insert-or-ignore into spawn_stamps.

    Idempotent: stamp_id is the sha256 of the raw line, so re-ingesting an
    already-seen line is a no-op — same contract as usage_events' PK.
    """
    mdir = manifest_dir or MANIFEST_DIR
    added = 0
    for path in sorted(mdir.glob("*.jsonl")) if mdir.is_dir() else []:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn write is not evidence; skip, never guess
            hint = rec.get("session_hint") or {}
            stamp_id = hashlib.sha256(line.encode("utf-8")).hexdigest()
            cur = conn.execute(
                "INSERT OR IGNORE INTO spawn_stamps"
                " (stamp_id, ts, orchestrator, provider, cwd, tmux_pane,"
                "  tmux_session, pid, bead_id, operator, raw)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    stamp_id,
                    rec.get("ts") or "",
                    rec.get("orchestrator") or "unknown",
                    rec.get("provider"),
                    hint.get("cwd"),
                    hint.get("tmux_pane"),
                    hint.get("tmux_session"),
                    hint.get("pid"),
                    rec.get("bead_id"),
                    rec.get("operator"),
                    line,
                ),
            )
            added += cur.rowcount
    conn.commit()
    return added


def annotate(conn: sqlite3.Connection) -> int:
    """Join unmatched stamps to sessions on recorded fields only.

    Rule 1 (preferred): provider equal AND cwd recorded on both sides and
        equal AND session first-event ts within [stamp.ts - 5s, +900s].
    Rule 2: provider equal AND the SESSION recorded no cwd AND ts window.
        (Recorded-absence is itself a recorded fact; the rule name says so.)

    Stamps and sessions pair off in ts order; one stamp, one session.
    """
    stamps = conn.execute(
        "SELECT stamp_id, ts, provider, cwd FROM spawn_stamps"
        " WHERE stamp_id NOT IN (SELECT stamp_id FROM session_delegation)"
        " ORDER BY ts"
    ).fetchall()
    if not stamps:
        return 0

    sessions = conn.execute(
        "SELECT provider, session_id, MIN(ts) AS start_ts, MIN(cwd) AS cwd"
        " FROM usage_events WHERE is_sidechain = 0"
        " GROUP BY provider, session_id"
        " HAVING start_ts IS NOT NULL"
        " ORDER BY start_ts"
    ).fetchall()
    claimed = {
        (p, s)
        for p, s in conn.execute(
            "SELECT provider, session_id FROM session_delegation"
        ).fetchall()
    }

    matched = 0
    for stamp_id, sts, sprov, scwd in stamps:
        t0 = _parse_ts(sts)
        if t0 is None or not sprov:
            continue
        lo = t0 - timedelta(seconds=WINDOW_BEFORE_S)
        hi = t0 + timedelta(seconds=WINDOW_AFTER_S)
        best = None  # (rule, start_ts, provider, session_id)
        for prov, sid, start_ts, cwd in sessions:
            if prov != sprov or (prov, sid) in claimed:
                continue
            t = _parse_ts(start_ts)
            if t is None or not (lo <= t <= hi):
                continue
            if scwd and cwd:
                if cwd == scwd:
                    rule = "provider+cwd+start-within-window"
                else:
                    continue
            elif cwd is None:
                rule = "provider+start-within-window (session recorded no cwd)"
            else:
                continue
            cand = (rule, start_ts, prov, sid)
            if best is None or cand[1] < best[1]:
                best = cand
        if best is None:
            continue
        rule, _, prov, sid = best
        conn.execute(
            "INSERT OR IGNORE INTO session_delegation"
            " (provider, session_id, stamp_id, matched_by) VALUES (?,?,?,?)",
            (prov, sid, stamp_id, rule),
        )
        claimed.add((prov, sid))
        matched += 1
    conn.commit()
    return matched


def report(conn: sqlite3.Connection) -> dict:
    """Per-provider session counts: stamped / native-sidechain / unclassified.

    'unclassified' is labelled attended-by-default: the absence of a stamp is
    reported as absence, never converted into a judgement about the session.
    """
    rows = conn.execute(
        """
        SELECT e.provider,
               COUNT(DISTINCT CASE WHEN d.session_id IS NOT NULL
                                   THEN e.session_id END) AS delegated,
               COUNT(DISTINCT CASE WHEN e.is_sidechain > 0
                                   THEN e.session_id END) AS sidechain,
               COUNT(DISTINCT CASE WHEN d.session_id IS NULL
                                    AND e.is_sidechain = 0
                                   THEN e.session_id END) AS unclassified
        FROM usage_events e
        LEFT JOIN session_delegation d
               ON d.provider = e.provider AND d.session_id = e.session_id
        GROUP BY e.provider ORDER BY e.provider
        """
    ).fetchall()
    stamps_total, stamps_matched = conn.execute(
        "SELECT (SELECT COUNT(*) FROM spawn_stamps),"
        " (SELECT COUNT(*) FROM session_delegation)"
    ).fetchone()
    return {
        "providers": [
            {
                "provider": p,
                "delegated_stamped": d,
                "delegated_native_sidechain": s,
                "unclassified_attended_by_default": u,
            }
            for p, d, s, u in rows
        ],
        "stamps": {"total": stamps_total, "matched": stamps_matched},
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="flightdeck-delegation",
        description="Ingest spawn-provenance stamps; join to sessions mechanically.",
    )
    p.add_argument("cmd", choices=["ingest", "annotate", "report", "run"])
    p.add_argument("--json", action="store_true", help="robot JSON output")
    args = p.parse_args(argv)

    conn = _connect()
    try:
        out: dict = {}
        if args.cmd in ("ingest", "run"):
            out["ingested"] = ingest(conn)
        if args.cmd in ("annotate", "run"):
            out["annotated"] = annotate(conn)
        if args.cmd in ("report", "run"):
            out["report"] = report(conn)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            if "ingested" in out:
                print(f"ingested {out['ingested']} new stamp(s)")
            if "annotated" in out:
                print(f"annotated {out['annotated']} session(s)")
            if "report" in out:
                r = out["report"]
                print(
                    f"stamps: {r['stamps']['matched']}/{r['stamps']['total']} matched"
                )
                for row in r["providers"]:
                    print(
                        f"  {row['provider']:8s}"
                        f" delegated={row['delegated_stamped']}"
                        f" sidechain={row['delegated_native_sidechain']}"
                        f" unclassified(attended-by-default)="
                        f"{row['unclassified_attended_by_default']}"
                    )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
