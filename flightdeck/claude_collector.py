"""Claude Code transcript collector.

Correctness rules (each one is a documented bug in a prior tool):
  * recurse into <session>/subagents/ AND <session>/subagents/workflows/wf_*/
  * a subagent file (isSidechain:true / agent-*.jsonl) is attributed to its
    PARENT sessionId and flagged is_sidechain=1 — it is stored once, so it can
    never be double-counted into session totals (the mission-control rglob bug)
  * cross-Mac synced sessions are deduped by (sessionId, message uuid), not by
    file path — the unique key (provider, session_id, event_id) enforces it
  * journal.jsonl is skipped
  * incremental by per-file mtime+size checkpoint (see checkpoint.py)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

SKIP_NAMES = {"journal.jsonl"}


def discover_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for f in sorted(root.rglob("*.jsonl")):
        if f.name in SKIP_NAMES:
            continue
        yield f


def fallback_session_id(path: Path) -> str | None:
    """Parent session id derived from layout when a record lacks sessionId.

    <proj>/<sess>.jsonl                                   -> <sess>
    <proj>/<sess>/subagents/agent-*.jsonl                 -> <sess>
    <proj>/<sess>/subagents/workflows/wf_*/agent-*.jsonl  -> <sess>
    """
    parts = path.parts
    if "subagents" in parts:
        i = parts.index("subagents")
        return parts[i - 1] if i >= 1 else None
    return path.stem


def is_sidechain_path(path: Path) -> bool:
    return path.name.startswith("agent-") or "subagents" in path.parts


def parse_file(path: Path) -> list[dict]:
    """Extract normalized usage rows from one Claude Code JSONL transcript."""
    rows: list[dict] = []
    fb_session = fallback_session_id(path)
    path_sidechain = is_sidechain_path(path)
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with fh:
        for line in fh:
            if '"usage"' not in line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict) or o.get("type") != "assistant":
                continue
            msg = o.get("message")
            if not isinstance(msg, dict):
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            model = msg.get("model") or ""
            if model == "<synthetic>":
                continue
            uuid = o.get("uuid")
            if not uuid:
                continue
            session_id = o.get("sessionId") or fb_session
            if not session_id:
                continue
            cc = u.get("cache_creation")
            cc = cc if isinstance(cc, dict) else {}
            rows.append(
                {
                    "session_id": session_id,
                    "event_id": uuid,
                    "model": model,
                    "ts": o.get("timestamp"),
                    "input_tokens": int(u.get("input_tokens") or 0),
                    "output_tokens": int(u.get("output_tokens") or 0),
                    "cache_creation_tokens": int(u.get("cache_creation_input_tokens") or 0),
                    "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
                    "cache_5m_tokens": int(cc.get("ephemeral_5m_input_tokens") or 0),
                    "cache_1h_tokens": int(cc.get("ephemeral_1h_input_tokens") or 0),
                    "reasoning_tokens": 0,
                    "is_sidechain": 1 if (o.get("isSidechain") or path_sidechain) else 0,
                    "agent_id": o.get("agentId"),
                    "cwd": o.get("cwd"),
                    "cost_micros": None,
                }
            )
    return rows


INSERT_SQL = (
    "INSERT OR IGNORE INTO usage_events (provider, account_root, session_id, event_id,"
    " model, ts, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
    " cache_5m_tokens, cache_1h_tokens, reasoning_tokens, is_sidechain, agent_id, cwd,"
    " cost_micros) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def insert_rows(conn, provider: str, account_root: str, rows: list[dict]) -> None:
    conn.executemany(
        INSERT_SQL,
        [
            (
                provider, account_root, r["session_id"], r["event_id"], r["model"],
                r["ts"], r["input_tokens"], r["output_tokens"], r["cache_creation_tokens"],
                r["cache_read_tokens"], r["cache_5m_tokens"], r["cache_1h_tokens"],
                r["reasoning_tokens"], r["is_sidechain"], r["agent_id"], r["cwd"],
                r["cost_micros"],
            )
            for r in rows
        ],
    )
