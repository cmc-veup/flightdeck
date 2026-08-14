"""SQLite storage for flightdeck.

Why not ~/.agentsview/sessions.db: its usage_events table has a NOT NULL
foreign key into agentsview's own sessions table (ON DELETE CASCADE) and cannot
express provider, account root, sidechain attribution, or the 5m/1h cache-write
split. Per the plan's escape hatch, flightdeck owns a clean schema in
~/.flightdeck/usage.db instead of mangling agentsview's.
"""

from __future__ import annotations

import contextlib
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
    service_tier  TEXT,                   -- codex thread_settings.service_tier; NULL = standard
    images        INTEGER NOT NULL DEFAULT 0,  -- gpt-image-* generations; COUNT only, see note
    PRIMARY KEY (provider, session_id, event_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events(ts);

-- Quota readings over time. `snapshots` is INSERT OR REPLACE and keeps only the
-- newest value, which answers "where am I now" but never "am I about to run
-- out" -- during an active swarm that is the only question that matters. Codex
-- stamps a rate_limits block into EVERY token_count event, so a full trajectory
-- is recoverable from rollouts already on disk rather than sampled from now on.
CREATE TABLE IF NOT EXISTS quota_samples (
    provider       TEXT NOT NULL,
    scope          TEXT NOT NULL,      -- primary | secondary
    ts             TEXT NOT NULL,      -- ISO8601 Z
    used_percent   REAL,
    window_minutes INTEGER,
    plan_type      TEXT,
    PRIMARY KEY (provider, scope, ts)
);
CREATE INDEX IF NOT EXISTS idx_quota_ts ON quota_samples(provider, ts);

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

# Codex priority ("fast") service tier bills at a multiple of the standard card.
# `~/.codex/config.toml`'s `service_tier` selects it and codex stamps the chosen
# tier into each rollout as `thread_settings.service_tier`. Values from ccusage's
# fast-multiplier-overrides.json, which is the only public table for these.
#
# Read the tier PER ROLLOUT, never from the live config: the config records what
# is set *now*, and projecting that backwards re-prices history that was billed
# at standard. On this estate `service_tier = "priority"` was added 2026-08-13
# (absent from config.toml.bak-20260805), so exactly 6 rollouts carry it —
# applying today's setting to all 2,805 would have overstated codex ~2x.
FAST_MULTIPLIER = {
    "gpt-5.6-sol": 2.0, "gpt-5.6-terra": 2.0, "gpt-5.6-luna": 2.0,
    "gpt-5.5": 2.5, "gpt-5.4": 2.0, "gpt-5.3-codex": 2.0,
}
FAST_TIERS = {"priority", "fast"}


def fast_multiplier(model: str | None, service_tier: str | None) -> float:
    """Multiplier for a row's service tier. 1.0 unless the rollout recorded fast."""
    if not model or not service_tier or service_tier.lower() not in FAST_TIERS:
        return 1.0
    # Longest matching pattern wins, same rule as the pricing table.
    hits = [(len(p), m) for p, m in FAST_MULTIPLIER.items() if p in model]
    return max(hits)[1] if hits else 1.0

# (pattern, in, out, cache_read, cache_write_5m, is_estimate, note,
#  valid_from, valid_to)
#
# is_estimate=1 means THE PUBLISHED RATE WAS NOT FOUND — not 'we did not
# get round to confirming it'. Anthropic, OpenAI, DeepSeek, Kimi and GLM-5.2
# rates below were read off their published cards on 2026-07-30 and are
# exact. Rows still flagged name what is missing in their note.
#
# Local models (ollama/qwen) carry a HOSTED-EQUIVALENT rate, not $0. The
# unit here is projected API cost; local inference is still inference, and
# pricing it at zero understates the estate. The cheapest credible
# comparable is used deliberately — reaching for the dearest would be
# gaming rather than accounting.
PRICING_SEED: list[tuple] = [
    ('<synthetic>', 0.0, 0.0, 0.0, 0.0, 0, "not a model - Claude Code internal marker", '', ''),
    ('claude-3-5-haiku', 0.8, 4.0, 0.08, 1.0, 0, "Haiku 3.5 retired - Anthropic published card, verified 2026-07-30", '', ''),
    ('claude-fable-5', 10.0, 50.0, 1.0, 12.5, 0, "Fable 5 - Anthropic published card, verified 2026-07-30", '', ''),
    ('claude-haiku-4', 1.0, 5.0, 0.1, 1.25, 0, "Haiku 4.5 - Anthropic published card, verified 2026-07-30", '', ''),
    ('claude-mythos', 10.0, 50.0, 1.0, 12.5, 0, "Mythos 5 - Anthropic published card, verified 2026-07-30", '', ''),
    ('claude-opus-4', 5.0, 25.0, 0.5, 6.25, 0, "Opus 4.5-4.8 - Anthropic published card, verified 2026-07-30", '', ''),
    ('claude-opus-4-1', 15.0, 75.0, 1.5, 18.75, 0, "Opus 4.1 deprecated - Anthropic published card, verified 2026-07-30", '', ''),
    ('claude-opus-5', 5.0, 25.0, 0.5, 6.25, 0, "Opus 5 - Anthropic published card, verified 2026-07-30", '', ''),
    ('claude-sonnet-3', 3.0, 15.0, 0.3, 3.75, 0, "Sonnet 3.x retired - Anthropic published card, verified 2026-07-30", '', ''),
    ('claude-sonnet-4', 3.0, 15.0, 0.3, 3.75, 0, "Sonnet 4-4.6 - Anthropic published card, verified 2026-07-30", '', ''),
    ('claude-sonnet-5', 2.0, 10.0, 0.2, 2.5, 0, "introductory, through 2026-08-31 - Anthropic published card, verified 2026-07-30", '', '2026-08-31'),
    ('claude-sonnet-5', 3.0, 15.0, 0.3, 3.75, 0, "standard from 2026-09-01 - Anthropic published card, verified 2026-07-30", '2026-09-01', ''),
    ('deepseek-v4-flash', 0.14, 0.28, 0.0028, 0.0, 0, "V4-Flash - DeepSeek published card, verified 2026-07-30", '', ''),
    ('deepseek-v4-pro', 0.435, 0.87, 0.003625, 0.0, 0, "V4-Pro - DeepSeek published card, verified 2026-07-30", '', ''),
    ('gemini-3-flash', 0.5, 3.0, 0.05, 0.0, 0, "Gemini 3 Flash published - verified 2026-07-30. Previous 0.30/2.50 was Gemini 2.5 Flash's rate left on a newer model. Cache read at the 10% convention.", '', ''),
    ('glm', 1.4, 4.4, 0.26, 0.0, 0, "GLM-5.1/5.2 Z.ai list - verified 2026-07-30. OpenRouter's 0.966/3.036 is the same card at 31% off; list is used to answer 'what would the API have charged'.", '', ''),
    ('glm-5.2', 1.4, 4.4, 0.26, 0.0, 0, "GLM-5.2 Z.ai list - verified 2026-07-30", '', ''),
    ('gpt-5.2', 1.75, 14.0, 0.175, 2.1875, 0, "OpenAI list; cache write at the 1.25x-input convention", '', ''),
    ('gpt-5.4', 2.5, 15.0, 0.25, 3.125, 0, "OpenAI list; cache write at the 1.25x-input convention", '2026-03-05', ''),
    ('gpt-5.4-mini', 0.75, 4.5, 0.075, 0.9375, 0, "OpenAI list; cache write at the 1.25x-input convention", '2026-03-05', ''),
    ('gpt-5.4-nano', 0.2, 1.25, 0.02, 0.25, 0, "OpenAI list; cache write at the 1.25x-input convention", '2026-03-05', ''),
    ('gpt-5.5', 5.0, 30.0, 0.5, 6.25, 0, "OpenAI list; cache write at the 1.25x-input convention", '2026-04-23', ''),
    # Cache write 6.25 verified 2026-08-05 against OpenAI's published card. NOT
    # modelled: requests over 272K input bill the WHOLE request at 2x input /
    # 1.5x output, and rollout files carry no per-request input size to detect it.
    ('gpt-5.6', 5.0, 30.0, 0.5, 6.25, 0, "OpenAI list (sol); cache write verified 2026-08-05", '2026-07-09', ''),
    # Input/output unconfirmed: a secondary source quotes luna 0.20/1.20 and
    # terra 2.00/12.00. No luna or terra usage on this estate, so left as-is
    # rather than changed on one source.
    ('gpt-5.6-luna', 1.0, 6.0, 0.1, 1.25, 0, "OpenAI list; cache write at the 1.25x-input convention", '2026-07-09', ''),
    ('gpt-5.6-terra', 2.5, 15.0, 0.25, 3.125, 0, "OpenAI list; cache write at the 1.25x-input convention", '2026-07-09', ''),
    # K3 is 5x the K2.5 card, and 'kimi' matches 'kimi-code/k3' by substring —
    # without this longer pattern the Kimi Code CLI would price at K2.5 rates
    # and under-report 5x. Verified 2026-08-14 on platform.kimi.ai/docs/pricing/chat-k3.
    ('kimi-code/k3', 3.0, 15.0, 0.30, 0.0, 0,
     "Kimi K3 - platform.kimi.ai published card, verified 2026-08-14: $3.00 input / $0.30 cache-hit / $15.00 output. No separate cache-write rate is published.", '', ''),
    ('kimi', 0.6, 3.0, 0.15, 0.0, 0, "Kimi K2.5 - Moonshot published card, verified 2026-07-30. Cache-hit input quoted 0.10-0.16 across providers; 0.15 used.", '', ''),
    ('minimax', 0.24, 0.96, 0.024, 0.0, 0, "MiniMax M2.7 published - verified 2026-07-30. Cache read at the 10% convention.", '', ''),
    ('claude-unknown', 5.0, 25.0, 0.5, 6.25, 1, "recovered rows w/o model; era's dominant tier (opus-4-6)", '', ''),
    ('gemini-3.1-pro', 1.25, 10.0, 0.125, 0.0, 1, "Gemini 3.1 Pro card not verified 2026-07-30 - needs a source", '', ''),
    ('gpt-5', 1.75, 14.0, 0.175, 2.1875, 1, "fallback for unlabelled gpt-5.x only; specific gpt-5.x rows are exact", '', ''),
    ('grok', 0.2, 0.5, 0.05, 0.0, 1, "grok reports cost_micros directly, so this row rarely applies", '', ''),
    ('ollama', 0.22, 1.8, 0.022, 0.0, 1, "hosted-equivalent for locally served models; see qwen note", '', ''),
    ('qwen', 0.22, 1.8, 0.022, 0.0, 1, "hosted-equivalent: Qwen3-Coder-480B on Alibaba Model Studio standard tier ($0.22/$1.80). Cache read assumed 10% of input - not published.", '', ''),
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
    # timeout IS sqlite's busy_timeout. Python's 5s default was not enough:
    # hourly collects died with "database is locked" at refresh_pricing's
    # commit whenever a concurrent full scan held its 250-file batch
    # transaction longer than 5s. 30s outlasts any observed batch window.
    conn = sqlite3.connect(str(path or DB_PATH), timeout=30.0)
    # WAL, because the default `delete` journal makes ANY writer block ALL
    # readers. Three schedulers touch this file -- the hourly device collect,
    # the 15-minute profile refresh (collect THEN badges), and hand runs -- so
    # a `total`, a `badges`, or a `doctor` overlapping a collect took the whole
    # run down with "database is locked" even though one side was only reading.
    # Under WAL readers never block the writer and the writer never blocks
    # readers; only writer-vs-writer remains, which collect's own lock covers.
    # Persistent once set, so this is a no-op after the first open. Local disk
    # only -- WAL is unsafe over network filesystems, and ~/.flightdeck is not.
    with contextlib.suppress(sqlite3.DatabaseError):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")   # WAL's durable-enough pairing
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
    # service_tier, added in place. CREATE TABLE IF NOT EXISTS above is a no-op
    # on an existing usage_events, so the column has to be ALTERed in or every
    # priority-tier row silently bills at standard on an upgraded database.
    ue_cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_events)")}
    if ue_cols and "service_tier" not in ue_cols:
        conn.execute("ALTER TABLE usage_events ADD COLUMN service_tier TEXT")
    if ue_cols and "images" not in ue_cols:
        conn.execute("ALTER TABLE usage_events ADD COLUMN images INTEGER NOT NULL DEFAULT 0")
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
    # Priority/fast tier multiplies the whole request, every bucket alike.
    usd *= fast_multiplier(row.get("model"), row.get("service_tier"))
    return usd, bool(est)
