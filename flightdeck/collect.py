"""`flightdeck collect` — incremental scan of all sources into usage.db."""

from __future__ import annotations

import os
import sys
import time

from . import checkpoint, claude_collector, codex_collector, grok_collector
from .db import open_db, refresh_pricing, utcnow_iso
from .paths import CLAUDE_ROOTS, CODEX_SESSIONS, GROK_DB

SAVE_EVERY = 250  # files between checkpoint flushes (crash safety)


def run(full: bool = False, quiet: bool = False) -> dict:
    t0 = time.time()
    conn = open_db()
    refresh_pricing(conn)
    ck = checkpoint.load()
    if full:
        ck = {"files": {}, "cursors": {}}
    stats = {"files_scanned": 0, "files_skipped": 0, "rows": 0, "by_source": {}}

    def log(msg: str) -> None:
        if not quiet:
            print(msg, file=sys.stderr, flush=True)

    # --- Claude-format roots (claude main/veup/pmme, deepseek, ollama) ---
    for root, provider, account in CLAUDE_ROOTS:
        if not root.is_dir():
            continue
        label = f"{provider}:{account}"
        n_files = n_rows = 0
        pending = 0
        for f in claude_collector.discover_files(root):
            try:
                st = os.stat(f)
            except OSError:
                continue
            key = str(f)
            if checkpoint.file_unchanged(ck, key, st):
                stats["files_skipped"] += 1
                continue
            rows = claude_collector.parse_file(f)
            claude_collector.insert_rows(conn, provider, account, rows)
            checkpoint.mark_file(ck, key, st)
            n_files += 1
            n_rows += len(rows)
            pending += 1
            if pending >= SAVE_EVERY:
                conn.commit()
                checkpoint.save(ck)
                pending = 0
                log(f"  {label}: {n_files} files, {n_rows} rows...")
        conn.commit()
        checkpoint.save(ck)
        stats["files_scanned"] += n_files
        stats["rows"] += n_rows
        stats["by_source"][label] = {"files": n_files, "rows": n_rows}
        log(f"{label}: {n_files} files parsed, {n_rows} usage rows")

    # --- Codex rollouts ---
    if CODEX_SESSIONS.is_dir():
        n_files = n_rows = 0
        latest_snapshot = None
        latest_snapshot_ts = ""
        for f in codex_collector.discover_files(CODEX_SESSIONS):
            try:
                st = os.stat(f)
            except OSError:
                continue
            key = str(f)
            if checkpoint.file_unchanged(ck, key, st):
                stats["files_skipped"] += 1
                continue
            row, snapshot = codex_collector.parse_file(f)
            if row is not None:
                codex_collector.insert_row(conn, row)
                n_rows += 1
            if snapshot is not None and (snapshot.get("timestamp") or "") > latest_snapshot_ts:
                latest_snapshot = snapshot
                latest_snapshot_ts = snapshot.get("timestamp") or ""
            checkpoint.mark_file(ck, key, st)
            n_files += 1
        if latest_snapshot is not None:
            codex_collector.save_snapshot(conn, latest_snapshot, utcnow_iso())
        conn.commit()
        checkpoint.save(ck)
        stats["files_scanned"] += n_files
        stats["rows"] += n_rows
        stats["by_source"]["codex"] = {"files": n_files, "rows": n_rows}
        log(f"codex: {n_files} rollouts parsed, {n_rows} usage rows")

    # --- Grok ---
    last_id = int(ck["cursors"].get("grok_last_id", 0))
    new_last = grok_collector.collect(conn, GROK_DB, last_id)
    if new_last != last_id:
        ck["cursors"]["grok_last_id"] = new_last
        conn.commit()
        checkpoint.save(ck)
    n_grok = new_last - last_id
    stats["by_source"]["grok"] = {"rows_new": n_grok}
    stats["rows"] += max(n_grok, 0)
    log(f"grok: {max(n_grok, 0)} new usage rows")

    conn.close()
    stats["elapsed_s"] = round(time.time() - t0, 1)
    log(f"collect done in {stats['elapsed_s']}s")
    return stats
