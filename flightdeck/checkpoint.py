"""Incremental-scan checkpoint: per-file (mtime, size) plus scalar cursors.

The Claude corpus is ~5.7GB and grows ~1.3GB/week; a full re-scan on every
collect is the failure mode this file exists to prevent. A file is reparsed
only when its mtime or size changed; the unique event key in SQLite makes
reparsing idempotent.
"""

from __future__ import annotations

import json
import os
import tempfile

from .paths import CHECKPOINT_PATH, ensure_dirs


def load() -> dict:
    try:
        with open(CHECKPOINT_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and "files" in data:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"files": {}, "cursors": {}}


def save(ck: dict) -> None:
    ensure_dirs()
    fd, tmp = tempfile.mkstemp(dir=str(CHECKPOINT_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(ck, f)
        os.replace(tmp, CHECKPOINT_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def file_unchanged(ck: dict, path: str, st: os.stat_result) -> bool:
    rec = ck["files"].get(path)
    return rec is not None and rec[0] == int(st.st_mtime) and rec[1] == st.st_size


def mark_file(ck: dict, path: str, st: os.stat_result) -> None:
    ck["files"][path] = [int(st.st_mtime), st.st_size]
