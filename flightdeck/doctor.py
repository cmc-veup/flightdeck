"""`flightdeck doctor` — data-source availability matrix."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import checkpoint
from .db import open_db
from .paths import CLAUDE_ROOTS, CODEX_SESSIONS, DB_PATH, GROK_DB


def _newest_mtime(root: Path, pattern: str) -> float | None:
    newest = None
    if not root.is_dir():
        return None
    for f in root.rglob(pattern):
        try:
            m = os.stat(f).st_mtime
        except OSError:
            continue
        newest = m if newest is None else max(newest, m)
    return newest


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gather() -> dict:
    conn = open_db()
    counts = dict(
        conn.execute(
            "SELECT provider || '/' || account_root, COUNT(*) FROM usage_events"
            " GROUP BY provider, account_root"
        ).fetchall()
    )
    snapshot = conn.execute(
        "SELECT payload, updated_at FROM snapshots WHERE provider='codex' AND key='rate_limits'"
    ).fetchone()
    conn.close()
    ck = checkpoint.load()

    sources = []
    for root, provider, account in CLAUDE_ROOTS:
        exists = root.is_dir()
        n_files = sum(1 for _ in root.rglob("*.jsonl")) if exists else 0
        sources.append(
            {
                "source": f"{provider}/{account}",
                "path": str(root),
                "exists": exists,
                "files": n_files,
                "freshest": _iso(_newest_mtime(root, "*.jsonl")),
                "db_rows": counts.get(f"{provider}/{account}", 0),
            }
        )
    sources.append(
        {
            "source": "codex/codex",
            "path": str(CODEX_SESSIONS),
            "exists": CODEX_SESSIONS.is_dir(),
            "files": sum(1 for _ in CODEX_SESSIONS.rglob("rollout-*.jsonl"))
            if CODEX_SESSIONS.is_dir() else 0,
            "freshest": _iso(_newest_mtime(CODEX_SESSIONS, "rollout-*.jsonl")),
            "db_rows": counts.get("codex/codex", 0),
        }
    )
    grok_exists = GROK_DB.is_file()
    grok_src_rows = 0
    grok_fresh = None
    if grok_exists:
        try:
            src = sqlite3.connect(f"file:{GROK_DB}?mode=ro", uri=True)
            grok_src_rows = src.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
            grok_fresh = src.execute("SELECT MAX(created_at) FROM usage_events").fetchone()[0]
            src.close()
        except sqlite3.Error:
            pass
    sources.append(
        {
            "source": "grok/grok",
            "path": str(GROK_DB),
            "exists": grok_exists,
            "files": grok_src_rows,
            "freshest": grok_fresh,
            "db_rows": counts.get("grok/grok", 0),
        }
    )
    return {
        "db": str(DB_PATH),
        "checkpoint_files": len(ck.get("files", {})),
        "codex_rate_limits": json.loads(snapshot[0]) if snapshot else None,
        "sources": sources,
    }


def run(as_json: bool = False) -> None:
    data = gather()
    if as_json:
        print(json.dumps(data, indent=2))
        return
    print(f"flightdeck doctor — db: {data['db']}  (checkpointed files: {data['checkpoint_files']})")
    print()
    w = max(len(s["source"]) for s in data["sources"]) + 2
    print(f"  {'source':<{w}}{'ok':<5}{'files':>8}{'db rows':>10}  freshest")
    for s in data["sources"]:
        ok = "yes" if s["exists"] else "NO"
        print(
            f"  {s['source']:<{w}}{ok:<5}{s['files']:>8}{s['db_rows']:>10}  "
            f"{s['freshest'] or '-'}"
        )
    rl = data["codex_rate_limits"]
    if rl:
        lim = rl.get("rate_limits", {})
        prim = lim.get("primary") or {}
        print()
        print(
            f"  codex plan: {rl.get('plan_type')}  primary window used: "
            f"{prim.get('used_percent')}%  (as of {rl.get('timestamp')})"
        )
