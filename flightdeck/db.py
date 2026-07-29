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
    model_pattern        TEXT NOT NULL,     -- longest substring match against model id wins
    valid_from           TEXT NOT NULL DEFAULT '',  -- '' = since forever
    valid_to             TEXT NOT NULL DEFAULT '',  -- '' = still current
    input_per_mtok       REAL NOT NULL,
    output_per_mtok      REAL NOT NULL,
    cache_read_per_mtok  REAL NOT NULL,
    cache_write_per_mtok REAL NOT NULL,     -- 5m TTL rate; 1h billed at 1.6x this
    is_estimate          INTEGER NOT NULL DEFAULT 0,
    note                 TEXT,
    updated_at           TEXT NOT NULL,
    -- keyed by window too: one model holds several rates over time, and a
    -- single-column key let an intro price overwrite its own sticker price
    PRIMARY KEY (model_pattern, valid_from)
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
    ("claude-fable-5",   10.0, 50.0, 1.00, 12.50, 0, "Anthropic list price", "", ""),
    ("claude-mythos",    10.0, 50.0, 1.00, 12.50, 0, "same as fable-5", "", ""),
    ("claude-opus-5",     5.0, 25.0, 0.50,  6.25, 0, "Anthropic list price", "", ""),
    ("claude-opus-4-1",  15.0, 75.0, 1.50, 18.75, 1, "legacy opus 4.1 tier", "", ""),
    ("claude-opus-4",     5.0, 25.0, 0.50,  6.25, 0, "opus 4.5-4.8", "", ""),
    # Tokens must be priced at the rate in force WHEN THEY WERE SPENT. Sonnet-5's
    # introductory rate runs through 2026-08-31, so everything so far bills at
    # $2/$10 — using the sticker price would overcharge it by 50%.
    ("claude-sonnet-5",   2.0, 10.0, 0.20,  2.50, 1, "intro pricing", "", "2026-08-31"),
    ("claude-sonnet-5",   3.0, 15.0, 0.30,  3.75, 1, "sticker after intro", "2026-09-01", ""),
    ("claude-sonnet-4",   3.0, 15.0, 0.30,  3.75, 0, "Anthropic list price", "", ""),
    ("claude-sonnet-3",   3.0, 15.0, 0.30,  3.75, 1, "retired tier", "", ""),
    ("claude-haiku-4",    1.0,  5.0, 0.10,  1.25, 0, "Anthropic list price", "", ""),
    ("claude-3-5-haiku",  0.8,  4.0, 0.08,  1.00, 1, "retired tier", "", ""),
    ("gpt-5.2",          1.75, 14.0, 0.175, 0.0,  0, "OpenAI list", "", ""),
    ("gpt-5.4-nano",     0.20,  1.25,0.02,  0.0,  0, "OpenAI list", "2026-03-05", ""),
    ("gpt-5.4-mini",     0.75,  4.5, 0.075, 0.0,  0, "OpenAI list", "2026-03-05", ""),
    ("gpt-5.4",          2.50, 15.0, 0.25,  0.0,  0, "OpenAI list", "2026-03-05", ""),
    ("gpt-5.5",          5.00, 30.0, 0.50,  0.0,  0, "OpenAI list", "2026-04-23", ""),
    ("gpt-5.6-luna",     1.00,  6.0, 0.10,  0.0,  0, "OpenAI list", "2026-07-09", ""),
    ("gpt-5.6-terra",    2.50, 15.0, 0.25,  0.0,  0, "OpenAI list", "2026-07-09", ""),
    ("gpt-5.6",          5.00, 30.0, 0.50,  0.0,  0, "OpenAI list (sol)", "2026-07-09", ""),
    ("gpt-5",            1.75, 14.0, 0.175, 0.0,  1, "fallback for unlabelled gpt-5.x", "", ""),
    ("grok",             0.20,  0.50, 0.05, 0.0,  1, "estimate; grok db reports cost_micros directly", "", ""),
    ("deepseek",         0.28,  1.10, 0.028, 0.0, 1, "deepseek-chat est.", "", ""),
    # Recovered archive rows carry no model id. They are entirely Feb-Mar 2026,
    # where claude-opus-4-6 was 88% of known Claude tokens — so the opus-4 tier
    # is the evidence for them, not a guess. Without this row 12.19B tokens
    # silently priced at $0 and the estate read $11k light.
    ("claude-unknown",   5.0, 25.0, 0.50,  6.25, 1, "recovered rows w/o model; era's dominant tier (opus-4-6)", "", ""),
    ("glm",              0.6,  2.2, 0.06,  0.0,  1, "Zhipu GLM est.", "", ""),
    ("kimi",             0.6,  2.5, 0.06,  0.0,  1, "Moonshot Kimi est.", "", ""),
    ("minimax",          0.3,  1.2, 0.03,  0.0,  1, "MiniMax est.", "", ""),
    ("gemini-3-flash",   0.3,  2.5, 0.03,  0.0,  1, "Gemini flash est.", "", ""),
    ("gemini-3.1-pro",   1.25,10.0, 0.125, 0.0,  1, "Gemini pro est.", "", ""),
    ("qwen",             0.0,  0.0, 0.0,   0.0,  0, "local via ollama - genuinely $0", "", ""),
    ("<synthetic>",      0.0,  0.0, 0.0,   0.0,  0, "not a model - Claude Code internal marker", "", ""),
    ("ollama",            0.0,   0.0, 0.0,  0.0,  0, "local inference, $0", "", ""),
]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _pricing_key_ok(conn) -> bool:
    """True when the pricing PK spans the validity window (not just the model)."""
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pricing'"
        ).fetchone()
    except Exception:
        return False
    return bool(sql) and "PRIMARY KEY (model_pattern, valid_from)" in (sql[0] or "")


def open_db(path=None) -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(str(path or DB_PATH))
    conn.executescript(SCHEMA)
    # Effective-dating, added in place for databases created before it existed.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pricing)")}
    has_window_key = any("valid_from" in (r[1] or "") for r in
                         conn.execute("PRAGMA index_list(pricing)")) or not cols
    if "valid_from" not in cols or not _pricing_key_ok(conn):
        # Old single-key table: a model could hold only ONE rate, so an intro
        # price silently replaced its sticker price. Rebuilt from the seed.
        conn.execute("DROP TABLE IF EXISTS pricing")
        conn.executescript(SCHEMA)
    conn.commit()
    return conn


def refresh_pricing(conn: sqlite3.Connection) -> None:
    now = utcnow_iso()
    conn.executemany(
        "INSERT OR REPLACE INTO pricing (model_pattern, input_per_mtok,"
        " output_per_mtok, cache_read_per_mtok, cache_write_per_mtok, is_estimate,"
        " note, updated_at, valid_from, valid_to) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(p, i, o, cr, cw, est, note, now, vf, vt)
         for p, i, o, cr, cw, est, note, vf, vt in PRICING_SEED],
    )
    conn.commit()


def load_pricing(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT model_pattern, input_per_mtok, output_per_mtok,"
        " cache_read_per_mtok, cache_write_per_mtok, is_estimate,"
        " valid_from, valid_to FROM pricing"
    ).fetchall()


def match_price(pricing: list[tuple], model: str | None, when: str | None = None):
    """Longest substring pattern wins, restricted to rates in force on `when`.

    Pricing is a function of time, not just model: introductory rates expire
    and list prices get cut. A row with no window is always valid, so undated
    rows keep working. `when` is an ISO date/timestamp; None means "today's
    rate", which is only correct for models whose price never moved.
    """
    if not model:
        return None
    day = (when or "")[:10]
    best = None
    for row in pricing:
        if row[0] not in model:
            continue
        vf = row[6] if len(row) > 6 else ""
        vt = row[7] if len(row) > 7 else ""
        if day:
            if vf and day < vf:
                continue
            if vt and day > vt:
                continue
        elif vt:
            continue          # no date given: prefer the currently-open rate
        if best is None or len(row[0]) > len(best[0]):
            best = row
    return best


def event_cost_usd(pricing: list[tuple], row: dict) -> tuple[float, bool]:
    """(usd, is_estimate) for one usage row. Provider-reported cost wins."""
    if row.get("cost_micros") is not None:
        return row["cost_micros"] / 1e6, False
    p = match_price(pricing, row.get("model"), row.get("ts") or row.get("day"))
    if p is None:
        return 0.0, True
    in_r, out_r, cr_r, cw_r, est = p[1], p[2], p[3], p[4], p[5]
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
