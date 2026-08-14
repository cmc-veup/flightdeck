"""Kimi Code CLI collector.

Reads ~/.kimi-code/sessions/<workspace>/session_<uuid>/agents/<agent>/wire.jsonl.
Token accounting lives in `usage.record` events carrying a four-way split:

    {"type": "usage.record", "model": "kimi-code/k3", "usageScope": "turn",
     "usage": {"inputOther": N, "output": N,
               "inputCacheRead": N, "inputCacheCreation": N}, "time": <ms>}

Those map 1:1 onto our columns, so no normalization is needed — unlike codex,
whose `input_tokens` includes the cached portion.

RECORDS ARE PER-TURN AND MUST BE SUMMED — the opposite of codex, where the last
`total_token_usage` is the whole rollout. Verified on disk rather than trusted
from the `usageScope: "turn"` label: `inputOther` across consecutive records in
one agent runs 3,078 / 1,075 / 3,577 / 343 / 422 — it varies freely instead of
climbing. Applying the codex last-wins rule here would keep one turn and discard
the rest of the session.

The trap worth naming: `inputCacheRead` in the same records DOES climb
(19,200 / 22,272 / 23,296 / 26,624 …) because the cached context grows every
turn. Sampling that one field alone looks exactly like a cumulative counter and
would have produced the wrong rule. Check a field that can go down.

Multi-agent: `agents/main` is the primary thread and `agents/agent-N` are
subagents, recorded in their own wire.jsonl files — the same fan-out dimension
Claude records via isSidechain, so it lands in is_sidechain the same way and
main-vs-subagent reporting works for kimi too.

Identity: wire.jsonl is append-only, so (agent, ordinal, time-ms) is stable
across rescans and unique per turn. `session_index.jsonl` maps each session to
its workDir, which becomes cwd.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def discover_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    yield from sorted(root.rglob("wire.jsonl"))


def load_workdirs(home: Path) -> dict[str, str]:
    """sessionId -> workDir, from ~/.kimi-code/session_index.jsonl."""
    out: dict[str, str] = {}
    idx = home / ".kimi-code" / "session_index.jsonl"
    if not idx.is_file():
        return out
    try:
        fh = open(idx, "r", encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict) and o.get("sessionId"):
                out[str(o["sessionId"])] = o.get("workDir") or ""
    return out


def session_and_agent(path: Path) -> tuple[str | None, str]:
    """(session dir name, agent slot) from .../session_<uuid>/agents/<agent>/wire.jsonl."""
    parts = path.parts
    agent = path.parent.name
    if "agents" in parts:
        i = parts.index("agents")
        if i >= 1:
            return parts[i - 1], agent
    return None, agent


def parse_file(path: Path, workdirs: dict[str, str] | None = None) -> list[dict]:
    """Normalized usage rows from one kimi-code wire.jsonl. One row per turn."""
    rows: list[dict] = []
    session_id, agent = session_and_agent(path)
    if not session_id:
        return rows
    cwd = (workdirs or {}).get(session_id) or None
    # agents/main is the primary thread; agents/agent-N are its fan-out.
    is_sidechain = 0 if agent == "main" else 1
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with fh:
        for ordinal, line in enumerate(fh):
            if '"usage.record"' not in line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict) or o.get("type") != "usage.record":
                continue
            u = o.get("usage")
            if not isinstance(u, dict):
                continue
            ms = o.get("time")
            ts = None
            if isinstance(ms, (int, float)):
                ts = (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
            rows.append(
                {
                    "session_id": session_id,
                    "event_id": f"{agent}:{ordinal}:{ms}",
                    "model": o.get("model") or "kimi-code/unknown",
                    "ts": ts,
                    "input_tokens": int(u.get("inputOther") or 0),
                    "output_tokens": int(u.get("output") or 0),
                    "cache_creation_tokens": int(u.get("inputCacheCreation") or 0),
                    "cache_read_tokens": int(u.get("inputCacheRead") or 0),
                    "cache_5m_tokens": 0,
                    "cache_1h_tokens": 0,
                    "reasoning_tokens": 0,
                    "is_sidechain": is_sidechain,
                    "agent_id": agent,
                    "cwd": cwd,
                    "cost_micros": None,
                    "service_tier": None,
                    "images": 0,
                }
            )
    return rows


INSERT_SQL = (
    "INSERT OR IGNORE INTO usage_events (provider, account_root, session_id, event_id,"
    " model, ts, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
    " cache_5m_tokens, cache_1h_tokens, reasoning_tokens, is_sidechain, agent_id, cwd,"
    " cost_micros, service_tier, images) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def insert_rows(conn, rows: list[dict]) -> None:
    conn.executemany(
        INSERT_SQL,
        [
            (
                "kimi", "kimi-code", r["session_id"], r["event_id"], r["model"],
                r["ts"], r["input_tokens"], r["output_tokens"],
                r["cache_creation_tokens"], r["cache_read_tokens"],
                r["cache_5m_tokens"], r["cache_1h_tokens"], r["reasoning_tokens"],
                r["is_sidechain"], r["agent_id"], r["cwd"], r["cost_micros"],
                r["service_tier"], r["images"],
            )
            for r in rows
        ],
    )
