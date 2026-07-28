"""Claude Code transcript collector.

Subagents are a primary dimension of the spend architecture: sessions fan out
agents, and a large share of all spend happens in those fan-outs. This
collector exists to measure that architecture correctly — every event is
classified main / subagent / workflow-subagent and attributed to the parent
sessionId, so main-vs-subagent spend can be reported per provider and model.

Correctness rules (each one is why prior tools could not see the
architecture):
  * recurse into <session>/subagents/ AND <session>/subagents/workflows/wf_*/
    — the workflow layout alone hid ~2,000 transcript files from every earlier
    scanner
  * a subagent file (isSidechain:true / agent-*.jsonl) is stored exactly once,
    attributed to its PARENT sessionId with its source class — the mechanism
    that makes the fan-out measurable is the same one that prevents the
    mission-control rglob double-count
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


def sidechain_class(path: Path) -> int:
    """0 = main-session file, 1 = plain subagent, 2 = workflow subagent.

    Workflow subagents live under subagents/workflows/wf_*/; plain subagents
    directly under subagents/. The value lands in the is_sidechain column
    (truthiness still means "sidechain", so 0/1 consumers keep working).
    """
    parts = path.parts
    if "subagents" in parts:
        i = parts.index("subagents")
        if i + 1 < len(parts) - 1 and parts[i + 1] == "workflows":
            return 2
        return 1
    if path.name.startswith("agent-"):
        return 1
    return 0


def parse_file(path: Path) -> list[dict]:
    """Extract normalized usage rows from one Claude Code JSONL transcript."""
    rows: list[dict] = []
    fb_session = fallback_session_id(path)
    path_class = sidechain_class(path)
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
                    "is_sidechain": path_class or (1 if o.get("isSidechain") else 0),
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
