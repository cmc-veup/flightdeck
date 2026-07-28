"""SQLite storage for flightdeck.

Why not ~/.agentsview/sessions.db: its usage_events table has a NOT NULL
foreign key into agentsview's own sessions table (ON DELETE CASCADE) and cannot
express provider, account root, sidechain attribution, or the 5m/1h cache-write
split. Per the plan's escape hatch, flightdeck owns a clean schema in
~/.flightdeck/usage.db instead of mangling agentsview's.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .paths import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    provider      TEXT NOT NULL,          -- claude|deepseek|ollama|codex|grok
    account_root  TEXT NOT NULL,          -- main|veup|pmme|deepseek|codex|grok...
    session_id    TEXT NOT NULL,          -- subagent events carry the PARENT session id
    event_id      TEXT NOT NULL,          -- message uuid / rollout marker / grok row id
    model         TEXT,
    ts            TEXT,                   -- ISO8601 Z
    input_tokens          INTEGER NOT NULL DEFAULT 0,  -- non-cached input
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_5m_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_1h_tokens       INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens      INTEGER NOT NULL DEFAULT 0,  -- codex reasoning_output_tokens
    is_sidechain  INTEGER NOT NULL DEFAULT 0,
    agent_id      TEXT,
    cwd           TEXT,
    cost_micros   INTEGER,                -- provider-reported cost (grok); NULL = compute from pricing
    PRIMARY KEY (provider, session_id, event_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events(ts);

CREATE TABLE IF NOT EXISTS pricing (
    model_pattern        TEXT PRIMARY KEY,  -- longest substring match against model id wins
    input_per_mtok       REAL NOT NULL,
    output_per_mtok      REAL NOT NULL,
    cache_read_per_mtok  REAL NOT NULL,
    cache_write_per_mtok REAL NOT NULL,     -- 5m TTL rate; 1h billed at 1.6x this
    is_estimate          INTEGER NOT NULL DEFAULT 0,
    note                 TEXT,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    provider   TEXT NOT NULL,
    key        TEXT NOT NULL,
    payload    TEXT NOT NULL,   -- JSON
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, key)
);
"""

# 1h cache-write = 2.0x base input; 5m = 1.25x base input -> 1h = 1.6x the 5m rate.
CACHE_1H_FACTOR = 1.6

# (pattern, in, out, cache_read, cache_write_5m, is_estimate, note)
# Anthropic rates from platform docs 2026-07 (cache read=0.1x in, write 5m=1.25x in).
PRICING_SEED: list[tuple[str, float, float, float, float, int, str]] = [
    ("claude-fable-5",   10.0, 50.0, 1.00, 12.50, 0, "Anthropic list price"),
    ("claude-mythos",    10.0, 50.0, 1.00, 12.50, 0, "same as fable-5"),
    ("claude-opus-5",     5.0, 25.0, 0.50,  6.25, 0, "Anthropic list price"),
    ("claude-opus-4-1",  15.0, 75.0, 1.50, 18.75, 1, "legacy opus 4.1 tier"),
    ("claude-opus-4",     5.0, 25.0, 0.50,  6.25, 0, "opus 4.5-4.8"),
    ("claude-sonnet-5",   3.0, 15.0, 0.30,  3.75, 1, "sticker $3/$15; intro $2/$10 thru 2026-08-31"),
    ("claude-sonnet-4",   3.0, 15.0, 0.30,  3.75, 0, "Anthropic list price"),
    ("claude-sonnet-3",   3.0, 15.0, 0.30,  3.75, 1, "retired tier"),
    ("claude-haiku-4",    1.0,  5.0, 0.10,  1.25, 0, "Anthropic list price"),
    ("claude-3-5-haiku",  0.8,  4.0, 0.08,  1.00, 1, "retired tier"),
    ("gpt-5",            1.25, 10.0, 0.125, 0.0,  1, "OpenAI gpt-5.x est.; cached input 0.1x; no write charge"),
    ("grok",             0.20,  0.50, 0.05, 0.0,  1, "estimate; grok db reports cost_micros directly"),
    ("deepseek",         0.28,  1.10, 0.028, 0.0, 1, "deepseek-chat est."),
    ("ollama",            0.0,   0.0, 0.0,  0.0,  0, "local inference, $0"),
]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def open_db(path=None) -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(str(path or DB_PATH))
    conn.executescript(SCHEMA)
    return conn


def refresh_pricing(conn: sqlite3.Connection) -> None:
    now = utcnow_iso()
    conn.executemany(
        "INSERT OR REPLACE INTO pricing VALUES (?,?,?,?,?,?,?,?)",
        [(p, i, o, cr, cw, est, note, now) for p, i, o, cr, cw, est, note in PRICING_SEED],
    )
    conn.commit()


def load_pricing(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT model_pattern, input_per_mtok, output_per_mtok,"
        " cache_read_per_mtok, cache_write_per_mtok, is_estimate FROM pricing"
    ).fetchall()


def match_price(pricing: list[tuple], model: str | None):
    """Longest substring pattern that appears in the model id wins."""
    if not model:
        return None
    best = None
    for row in pricing:
        if row[0] in model and (best is None or len(row[0]) > len(best[0])):
            best = row
    return best


def event_cost_usd(pricing: list[tuple], row: dict) -> tuple[float, bool]:
    """(usd, is_estimate) for one usage row. Provider-reported cost wins."""
    if row.get("cost_micros") is not None:
        return row["cost_micros"] / 1e6, False
    p = match_price(pricing, row.get("model"))
    if p is None:
        return 0.0, True
    _, in_r, out_r, cr_r, cw_r, est = p
    c5, c1 = row.get("cache_5m_tokens", 0), row.get("cache_1h_tokens", 0)
    cc = row.get("cache_creation_tokens", 0)
    if c5 + c1 > 0:
        write_cost = c5 * cw_r + c1 * cw_r * CACHE_1H_FACTOR
    else:
        write_cost = cc * cw_r  # no TTL split recorded: bill at 5m rate
    usd = (
        row.get("input_tokens", 0) * in_r
        + row.get("output_tokens", 0) * out_r
        + row.get("cache_read_tokens", 0) * cr_r
        + write_cost
    ) / 1e6
    return usd, bool(est)
