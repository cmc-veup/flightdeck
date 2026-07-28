"""Codex CLI rollout collector.

Reads ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. The token accounting lives
in `token_count` event_msg payloads whose `total_token_usage` is CUMULATIVE for
the rollout — so the LAST token_count per file is the truth (prior tools either
counted characters or capped reads at 100 lines/file). One row per rollout,
INSERT OR REPLACE so a still-growing rollout stays correct on re-scan.

Normalization: codex `input_tokens` INCLUDES `cached_input_tokens`; we store
input = input - cached and cache_read = cached so the columns mean the same
thing as Anthropic's. `reasoning_output_tokens` is a subset of output_tokens
and is kept in its own informational column.

Also captures the latest rate_limits/plan_type payload as a snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


def discover_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    yield from sorted(root.rglob("rollout-*.jsonl"))


def parse_file(path: Path) -> tuple[dict | None, dict | None]:
    """Returns (usage_row | None, rate_limits_snapshot | None)."""
    meta: dict = {}
    model: str | None = None
    last_tc: dict | None = None
    last_ts: str | None = None
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    with fh:
        for line in fh:
            if not line.startswith("{"):
                continue
            fast = ('"session_meta"' in line or '"turn_context"' in line
                    or '"token_count"' in line)
            if not fast:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            payload = o.get("payload") if isinstance(o.get("payload"), dict) else {}
            if t == "session_meta":
                meta = payload
            elif t == "turn_context":
                model = payload.get("model") or model
            elif t == "event_msg" and payload.get("type") == "token_count":
                last_tc = payload
                last_ts = o.get("timestamp")

    if last_tc is None:
        return None, None
    info = last_tc.get("info") if isinstance(last_tc.get("info"), dict) else {}
    tu = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
    if not tu:
        return None, None
    inp = int(tu.get("input_tokens") or 0)
    cached = int(tu.get("cached_input_tokens") or 0)
    row = {
        "session_id": meta.get("id") or path.stem,
        "event_id": "rollout-total",
        "model": model or meta.get("model_provider") or "gpt-5",
        "ts": last_ts,
        "input_tokens": max(inp - cached, 0),
        "output_tokens": int(tu.get("output_tokens") or 0),
        "cache_creation_tokens": 0,
        "cache_read_tokens": cached,
        "cache_5m_tokens": 0,
        "cache_1h_tokens": 0,
        "reasoning_tokens": int(tu.get("reasoning_output_tokens") or 0),
        "is_sidechain": 0,
        "agent_id": None,
        "cwd": meta.get("cwd"),
        "cost_micros": None,
    }
    snapshot = None
    rl = last_tc.get("rate_limits")
    if isinstance(rl, dict):
        snapshot = {"timestamp": last_ts, "rate_limits": rl,
                    "plan_type": rl.get("plan_type")}
    return row, snapshot


REPLACE_SQL = (
    "INSERT OR REPLACE INTO usage_events (provider, account_root, session_id, event_id,"
    " model, ts, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
    " cache_5m_tokens, cache_1h_tokens, reasoning_tokens, is_sidechain, agent_id, cwd,"
    " cost_micros) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def insert_row(conn, row: dict) -> None:
    conn.execute(
        REPLACE_SQL,
        (
            "codex", "codex", row["session_id"], row["event_id"], row["model"],
            row["ts"], row["input_tokens"], row["output_tokens"],
            row["cache_creation_tokens"], row["cache_read_tokens"],
            row["cache_5m_tokens"], row["cache_1h_tokens"], row["reasoning_tokens"],
            row["is_sidechain"], row["agent_id"], row["cwd"], row["cost_micros"],
        ),
    )


def save_snapshot(conn, snapshot: dict, now_iso: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (provider, key, payload, updated_at)"
        " VALUES ('codex', 'rate_limits', ?, ?)",
        (json.dumps(snapshot), now_iso),
    )
