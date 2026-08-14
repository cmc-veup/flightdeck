"""`flightdeck collect` — incremental scan of all sources into usage.db."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import sys
import time

from . import (checkpoint, claude_collector, codex_collector, grok_collector,
               kimi_collector)
from .db import open_db, refresh_pricing, utcnow_iso
from .paths import (CLAUDE_ROOTS, CODEX_ROOTS, FLIGHTDECK_DIR, GROK_DB,
                    KIMI_SESSIONS, HOME)

SAVE_EVERY = 250  # files between checkpoint flushes (crash safety)
LOCK_PATH = FLIGHTDECK_DIR / "collect.lock"


@contextlib.contextmanager
def _single_writer(wait_s: float = 0.0):
    """Serialize collects across processes. Yields True if we hold the lock.

    Three schedulers call collect on this machine -- the hourly device job, the
    15-minute profile refresh, and hand runs -- so overlap is routine, not
    exotic. Two collects against one SQLite file produce
    `OperationalError: database is locked`: the loser dies mid-scan, and
    because a file is only checkpointed AFTER its rows commit, its work is
    simply redone next pass. Nothing is corrupted and nothing is lost, but the
    run that died reports failure and any caller chained behind it (badge
    regeneration, a push) is skipped.

    The lock lives in the library rather than in each caller precisely because
    the third writer is a human at a prompt -- a wrapper script cannot protect
    an invocation that does not go through it.

    Advisory, non-blocking by default: if another collect is already running,
    say so and skip rather than queue. Collect is incremental and idempotent,
    so a skipped run costs nothing -- the next one picks up the same files.
    """
    fh = open(LOCK_PATH, "w")
    deadline = time.time() + wait_s
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.time() >= deadline:
                    yield False
                    return
                time.sleep(0.25)
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def run(full: bool = False, quiet: bool = False) -> dict:
    with _single_writer() as acquired:
        if not acquired:
            if not quiet:
                print("collect: another collect is running — skipping this pass",
                      file=sys.stderr, flush=True)
            return {"files_scanned": 0, "files_skipped": 0, "rows": 0,
                    "by_source": {}, "skipped_locked": True}
        return _run_locked(full=full, quiet=quiet)


def _run_locked(full: bool = False, quiet: bool = False) -> dict:
    t0 = time.time()
    conn = open_db()
    refresh_pricing(conn)
    ck = checkpoint.load()
    if full:
        # every source is rebuildable from disk, so a full run is a true
        # rebuild: wipe rows so reclassifications (e.g. source class) apply
        # instead of being silently ignored by INSERT OR IGNORE
        ck = {"files": {}, "cursors": {}}
        conn.execute("DELETE FROM usage_events")
        conn.commit()
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

    # --- Kimi Code CLI ---
    if KIMI_SESSIONS.is_dir():
        n_files = n_rows = 0
        workdirs = kimi_collector.load_workdirs(HOME)
        for f in kimi_collector.discover_files(KIMI_SESSIONS):
            try:
                st = os.stat(f)
            except OSError:
                continue
            key = str(f)
            if checkpoint.file_unchanged(ck, key, st):
                stats["files_skipped"] += 1
                continue
            rows = kimi_collector.parse_file(f, workdirs)
            kimi_collector.insert_rows(conn, rows)
            checkpoint.mark_file(ck, key, st)
            n_files += 1
            n_rows += len(rows)
        conn.commit()
        checkpoint.save(ck)
        stats["files_scanned"] += n_files
        stats["rows"] += n_rows
        stats["by_source"]["kimi"] = {"files": n_files, "rows": n_rows}
        log(f"kimi: {n_files} files parsed, {n_rows} usage rows")

    # --- Codex rollouts (one pass per discovered account home) ---
    for codex_root, codex_account in CODEX_ROOTS:
        n_files = n_rows = 0
        latest_snapshot = None
        latest_snapshot_ts = ""
        for f in codex_collector.discover_files(codex_root):
            try:
                st = os.stat(f)
            except OSError:
                continue
            key = str(f)
            if checkpoint.file_unchanged(ck, key, st):
                stats["files_skipped"] += 1
                continue
            try:
                row, snapshot, quota = codex_collector.parse_file(f)
            except OSError:
                # Unreadable this pass: leave it UNMARKED so the next collect
                # retries. Marking it anyway (the old fail-open behavior) froze
                # the file out of every future incremental run.
                continue
            if row is not None:
                codex_collector.insert_row(conn, row, codex_account)
                n_rows += 1
            codex_collector.insert_quota(conn, quota)
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
        stats["by_source"][f"codex:{codex_account}"] = {"files": n_files, "rows": n_rows}
        log(f"codex:{codex_account}: {n_files} rollouts parsed, {n_rows} usage rows")

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
