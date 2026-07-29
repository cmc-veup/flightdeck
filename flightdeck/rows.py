"""Portable row export/import — git as the transport, no rsync, no daemon.

The multi-device pattern this enables:

    # on every device, on a schedule
    flightdeck collect
    flightdeck export --rows --out repo/devices/$(hostname -s).jsonl
    cd repo && git add -A && git commit -m sync && git push

    # anywhere you want the combined number
    git pull
    for f in devices/*.jsonl; do flightdeck merge "$f"; done
    flightdeck total

Why it works without conflicts: each device writes **only its own file**, so
two machines pushing at once touch different paths and git never has to merge
anything. NDJSON (not the SQLite file) because it diffs, compresses, and
survives a three-way merge if one ever does happen.

Why it is safe to re-run: rows are keyed `(provider, session_id, event_id)`,
so importing the same file twice is a no-op and a session that synced to two
devices lands once.

**`cwd` is redacted by default.** Working directories carry client and project
names — `~/vc/<customer>/...` — and this file is designed to leave the
machine. `--include-cwd` opts back in for a private repo you control.
"""

from __future__ import annotations

import json
from pathlib import Path

from .db import open_db

FIELDS = (
    "provider", "account_root", "session_id", "event_id", "model", "ts",
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
    "cache_5m_tokens", "cache_1h_tokens", "reasoning_tokens", "is_sidechain",
    "agent_id", "cwd", "cost_micros",
)


def export_rows(out: str | Path, db_path=None, include_cwd: bool = False,
                since: str | None = None) -> dict:
    conn = open_db(db_path)
    where, params = "", []
    if since:
        where, params = "WHERE ts >= ?", [since]
    cur = conn.execute(f"SELECT {', '.join(FIELDS)} FROM usage_events {where}", params)
    path = Path(out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    n = tokens = 0
    with path.open("w") as fh:
        for row in cur:
            rec = dict(zip(FIELDS, row))
            if not include_cwd:
                rec.pop("cwd", None)
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            n += 1
            tokens += sum(rec.get(k) or 0 for k in (
                "input_tokens", "output_tokens",
                "cache_creation_tokens", "cache_read_tokens"))
    return {"out": str(path), "events": n, "tokens": tokens,
            "cwd_included": include_cwd}


def import_rows(path: str | Path, db_path=None, device: str | None = None) -> dict:
    """Load an NDJSON row file. Duplicate events collide on the primary key."""
    src = Path(path).expanduser()
    if not src.exists():
        raise SystemExit(f"no row file at {src}")
    conn = open_db(db_path)
    before = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens+output_tokens"
        "+cache_creation_tokens+cache_read_tokens),0) FROM usage_events"
    ).fetchone()
    batch = []
    for line in src.open(errors="ignore"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if device and rec.get("account_root"):
            rec["account_root"] = f"{device}:{rec['account_root']}"
        batch.append(tuple(rec.get(f) for f in FIELDS))
        if len(batch) >= 5000:
            _flush(conn, batch)
            batch = []
    if batch:
        _flush(conn, batch)
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens+output_tokens"
        "+cache_creation_tokens+cache_read_tokens),0) FROM usage_events"
    ).fetchone()
    return {"source": str(src), "new_events": after[0] - before[0],
            "new_tokens": after[1] - before[1],
            "total_events": after[0], "total_tokens": after[1]}


def _flush(conn, batch) -> None:
    conn.executemany(
        f"INSERT OR IGNORE INTO usage_events ({', '.join(FIELDS)}) "
        f"VALUES ({','.join('?' * len(FIELDS))})", batch)
