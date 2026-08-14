"""Codex CLI rollout collector.

Reads ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. The token accounting lives
in `token_count` event_msg payloads whose `total_token_usage` is CUMULATIVE for
the rollout — so the LAST token_count per file is the truth (prior tools either
counted characters or capped reads at 100 lines/file). One row per rollout,
INSERT OR REPLACE so a still-growing rollout stays correct on re-scan.

Identity: one rollout FILE is one run, keyed by the UUID embedded in its
filename (rollout-<timestamp>-<uuid>.jsonl). It must NOT be keyed by the last
session_meta seen in the stream: forked/resumed rollouts (codex exec) replay
their parent's history INCLUDING the parent's session_meta line, so many files
share a trailing session id. Keying on that id collapsed whole fleets of runs
onto one (provider, session_id, event_id) row — 121 Aug-5 rollouts became 26 —
and INSERT OR REPLACE kept only the last-scanned file's totals (~75% of codex
burn silently dropped). The filename UUID equals the file's OWN (first)
session_meta id and is unique per file by construction.

Normalization: codex `input_tokens` INCLUDES `cached_input_tokens`; we store
input = input - cached and cache_read = cached so the columns mean the same
thing as Anthropic's. `reasoning_output_tokens` is a subset of output_tokens
and is kept in its own informational column.

Images are COUNTED, never priced. `total_token_usage` tracks the AGENT model
only: measured on a controlled single-image probe, the counter grew +23,602,
+25,196, +31,979, +34,513 input across four turns with the generation sitting
between the third and fourth — flat, no spike attributable to the image. The
`image_generation_end` event carries only call_id/result/saved_path, and
gpt-image-2's own tokens are billed by OpenAI with no local record anywhere.
So the agent side of an image (~135K input for one image) IS captured as
normal rollout tokens, and the image model's side is unmeasurable from disk —
the same class of hole as Gemini. Recording the count keeps that hole VISIBLE
instead of silently absent; inventing a token estimate for it would not.

Also captures the latest rate_limits/plan_type payload as a snapshot.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

_FILENAME_UUID = re.compile(
    r"-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


def rollout_id(path: Path) -> str:
    """Stable per-file identity: the UUID from rollout-<timestamp>-<uuid>.jsonl.

    Falls back to the full stem when the name carries no UUID — still unique
    per file, which is the invariant that matters.
    """
    m = _FILENAME_UUID.search(path.stem)
    return m.group(1) if m else path.stem


def discover_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    yield from sorted(root.rglob("rollout-*.jsonl"))


def parse_file(path: Path) -> tuple[dict | None, dict | None, list[dict]]:
    """Returns (usage_row | None, rate_limits_snapshot | None, quota_samples).

    Raises OSError when the file cannot be opened, so the caller can leave it
    unmarked in the checkpoint and retry next collect (fail-closed). Swallowing
    it here meant the file got checkpointed as done and its tokens were lost to
    every future incremental run.
    """
    meta: dict = {}
    model: str | None = None
    last_tc: dict | None = None
    last_ts: str | None = None
    service_tier: str | None = None
    images = 0
    samples: list[dict] = []
    fh = open(path, "r", encoding="utf-8", errors="replace")
    with fh:
        for line in fh:
            if not line.startswith("{"):
                continue
            if '"image_generation_end"' in line:
                images += 1          # counted from the raw line: cheap, and the
                                     # event carries nothing else worth parsing
            fast = ('"session_meta"' in line or '"turn_context"' in line
                    or '"token_count"' in line or '"thread_settings"' in line)
            if not fast:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            payload = o.get("payload") if isinstance(o.get("payload"), dict) else {}
            if t == "session_meta":
                # FIRST wins: the file's own meta. Later session_meta lines are
                # the PARENT session's, replayed into forked/resumed rollouts.
                if not meta:
                    meta = payload
            elif t == "turn_context":
                model = payload.get("model") or model
            elif t == "event_msg" and payload.get("type") == "token_count":
                last_tc = payload
                last_ts = o.get("timestamp")
                # Keep EVERY rate_limits reading, not just the final one: the
                # series is what turns "19% right now" into "19% and climbing
                # at N%/hr", which is the only form that predicts the wall.
                rl_now = payload.get("rate_limits")
                if isinstance(rl_now, dict) and o.get("timestamp"):
                    for scope in ("primary", "secondary"):
                        w = rl_now.get(scope)
                        if isinstance(w, dict) and w.get("used_percent") is not None:
                            samples.append({
                                "scope": scope, "ts": o["timestamp"],
                                "used_percent": w.get("used_percent"),
                                "window_minutes": w.get("window_minutes"),
                                "plan_type": rl_now.get("plan_type"),
                            })
            # thread_settings.service_tier is how codex stamps the tier the run
            # actually used ("priority" = fast, billed at a multiple). Read it
            # from the ROLLOUT, never from ~/.codex/config.toml: the config says
            # what is set now, and back-projecting it re-prices history that was
            # billed at standard. Absent = standard, which is the safe default.
            if service_tier is None:
                ts_block = payload.get("thread_settings")
                if isinstance(ts_block, dict) and ts_block.get("service_tier"):
                    service_tier = str(ts_block["service_tier"])

    if last_tc is None:
        return None, None, samples
    info = last_tc.get("info") if isinstance(last_tc.get("info"), dict) else {}
    tu = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
    if not tu:
        return None, None, samples
    inp = int(tu.get("input_tokens") or 0)
    cached = int(tu.get("cached_input_tokens") or 0)
    row = {
        "session_id": rollout_id(path),
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
        "service_tier": service_tier,
        "images": images,
    }
    snapshot = None
    rl = last_tc.get("rate_limits")
    if isinstance(rl, dict):
        snapshot = {"timestamp": last_ts, "rate_limits": rl,
                    "plan_type": rl.get("plan_type")}
    return row, snapshot, samples


REPLACE_SQL = (
    "INSERT OR REPLACE INTO usage_events (provider, account_root, session_id, event_id,"
    " model, ts, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
    " cache_5m_tokens, cache_1h_tokens, reasoning_tokens, is_sidechain, agent_id, cwd,"
    " cost_micros, service_tier, images) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def insert_row(conn, row: dict, account: str = "codex") -> None:
    conn.execute(
        REPLACE_SQL,
        (
            "codex", account, row["session_id"], row["event_id"], row["model"],
            row["ts"], row["input_tokens"], row["output_tokens"],
            row["cache_creation_tokens"], row["cache_read_tokens"],
            row["cache_5m_tokens"], row["cache_1h_tokens"], row["reasoning_tokens"],
            row["is_sidechain"], row["agent_id"], row["cwd"], row["cost_micros"],
            row.get("service_tier"), row.get("images", 0),
        ),
    )


def save_snapshot(conn, snapshot: dict, now_iso: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (provider, key, payload, updated_at)"
        " VALUES ('codex', 'rate_limits', ?, ?)",
        (json.dumps(snapshot), now_iso),
    )


QUOTA_SQL = (
    "INSERT OR IGNORE INTO quota_samples (provider, scope, ts, used_percent,"
    " window_minutes, plan_type) VALUES ('codex',?,?,?,?,?)"
)


def insert_quota(conn, samples: list[dict]) -> None:
    if not samples:
        return
    conn.executemany(QUOTA_SQL, [
        (s["scope"], s["ts"], s["used_percent"], s["window_minutes"], s["plan_type"])
        for s in samples
    ])


def record_auth_identity(conn, home, now_iso: str) -> dict | None:
    """Sample who ~/.codex/auth.json currently points at, and upsert the window.

    The account is only in the id_token JWT -- auth.json's plain fields carry a
    `tokens.account_id` but no email or plan, and a rollout carries no identity
    at all. Decoding the payload is read-only and needs no verification: we are
    reading our own local file to label our own data, not authenticating anyone.
    """
    import base64
    p = Path(home) / ".codex" / "auth.json"
    try:
        d = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    tok = d.get("tokens") or {}
    acct = tok.get("account_id")
    email = plan = None
    idt = tok.get("id_token")
    if idt and idt.count(".") == 2:
        try:
            body = idt.split(".")[1]
            body += "=" * (-len(body) % 4)
            claims = json.loads(base64.urlsafe_b64decode(body))
            email = claims.get("email")
            auth = claims.get("https://api.openai.com/auth") or {}
            acct = auth.get("chatgpt_account_id") or acct
            plan = auth.get("chatgpt_plan_type")
        except Exception:
            pass
    if not acct:
        return None
    conn.execute(
        "INSERT INTO codex_auth_history (account_id, email, plan_type, first_seen, last_seen)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(account_id) DO UPDATE SET last_seen=excluded.last_seen,"
        " email=COALESCE(excluded.email, email), plan_type=COALESCE(excluded.plan_type, plan_type)",
        (acct, email, plan, now_iso, now_iso),
    )
    return {"account_id": acct, "email": email, "plan_type": plan}
