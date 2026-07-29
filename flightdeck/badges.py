"""Live profile telemetry — shields.io endpoints and a self-updating SVG chart.

A GitHub profile README cannot run JavaScript, so "live" has to mean two
things it *can* render: shields.io endpoint badges (any public JSON URL) and a
committed SVG. Regenerate both on a timer, commit, and the profile reports
itself instead of rotting.

Every badge here is a number this tool can defend. Two were deliberately left
out after the data refused to support them:

* **Concurrent agents as a headline.** Inferring concurrency from token events
  produced a peak of 1,000 sessions on 2026-05-23 — and every one of those
  sessions had exactly ONE event that day (median 1). That is the recovery
  index having salvaged a single message from many short-lived sessions, not a
  thousand agents working. The same measure on 2026-07-28 gives 128 sessions of
  which 110 sustained >=5 events, median 24. So concurrency is only reported
  with the sustained filter applied, and only for windows whose transcripts are
  intact.
* **"Multimodal".** The swarm is multi-MODEL (24 models, 4 providers), not
  multimodal (text/image/audio). The wrong word invites the one correction you
  never want on a public claim.
"""

from __future__ import annotations

import collections
import json
import sqlite3
from pathlib import Path

from .db import open_db
from .reconcile import totals as estate_totals

SHIELD_COLOR = "2b2b2b"          # matches the flat-square dark convention
SUSTAINED_EVENTS = 5             # a "real" concurrent session, not a one-shot


def _shield(label: str, message: str, color: str = SHIELD_COLOR) -> dict:
    return {"schemaVersion": 1, "label": label, "message": message,
            "color": color, "style": "flat-square"}


def _human(n: float) -> str:
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return str(int(n))


def peak_concurrency(conn, day: str | None = None) -> tuple[int, int]:
    """(sustained, raw) peak sessions in a 10-minute window.

    `sustained` counts only sessions with >= SUSTAINED_EVENTS events that day,
    which is what separates a real swarm from an artifact of partial recovery.
    """
    where, params = "", []
    if day:
        where, params = "WHERE ts LIKE ?", [day + "%"]
    rows = conn.execute(
        f"SELECT session_id, ts FROM usage_events {where or 'WHERE ts IS NOT NULL'}",
        params).fetchall()
    per = collections.Counter(s for s, _ in rows)
    buckets: dict[str, set] = collections.defaultdict(set)
    for sid, ts in rows:
        if ts:
            buckets[ts[:15]].add(sid)
    if not buckets:
        return 0, 0
    best_raw = max(len(v) for v in buckets.values())
    best_sus = max(
        (sum(1 for s in v if per[s] >= SUSTAINED_EVENTS) for v in buckets.values()),
        default=0)
    return best_sus, best_raw


# The `provider` column records which CONFIG DIR a session ran in, not who made
# the model — anything driven through a Claude Code shell reads as "claude" or
# "deepseek". Counting it reported 4 vendors when the models name 9. Vendor is
# a property of the model id, so derive it there.
VENDOR_PREFIXES = (
    ("claude", "Anthropic"), ("gpt", "OpenAI"), ("o1", "OpenAI"),
    ("gemini", "Google"), ("grok", "xAI"), ("deepseek", "DeepSeek"),
    ("glm", "Zhipu"), ("kimi", "Moonshot"), ("qwen", "Alibaba"),
    ("minimax", "MiniMax"), ("llama", "Meta"), ("mistral", "Mistral"),
)


def vendor_of(model: str | None) -> str | None:
    if not model:
        return None
    m = model.lower()
    for prefix, name in VENDOR_PREFIXES:
        if m.startswith(prefix):
            return name
    return None


def count_vendors(conn) -> int:
    rows = conn.execute(
        "SELECT DISTINCT model FROM usage_events WHERE model IS NOT NULL").fetchall()
    return len({v for (m,) in rows if (v := vendor_of(m))})


def _window_start(conn, days: int) -> str:
    last = conn.execute(
        "SELECT MAX(substr(ts,1,10)) FROM usage_events WHERE ts IS NOT NULL").fetchone()[0]
    if not last:
        return "0000-00-00"
    import datetime
    d = datetime.date.fromisoformat(last) - datetime.timedelta(days=days)
    return d.isoformat()


def collect_metrics(conn, rank: int | None = None, tier: str | None = None,
                    window_days: int = 30) -> dict:
    tok = "input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens"
    est = estate_totals(conn)
    measured = est["per_event_main"] + est["per_event_subagent"]
    models = conn.execute(
        "SELECT COUNT(DISTINCT model) FROM usage_events WHERE model IS NOT NULL"
        " AND model NOT IN ('<synthetic>','unknown','claude-unknown')").fetchone()[0]
    vendors = count_vendors(conn)
    # Badges describe how the estate runs NOW. Today is partial and the
    # all-time figures are dragged down by months whose transcripts were
    # deleted before they could be indexed, so both use a trailing window.
    since = _window_start(conn, window_days)
    peak_day, sustained = None, 0
    for (d,) in conn.execute(
            "SELECT DISTINCT substr(ts,1,10) FROM usage_events"
            " WHERE ts >= ? ORDER BY 1", (since,)):
        s_day, _ = peak_concurrency(conn, d)
        if s_day > sustained:
            sustained, peak_day = s_day, d
    win = conn.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN is_sidechain>0 THEN {tok} ELSE 0 END),0),
                   COALESCE(SUM({tok}),0)
            FROM usage_events WHERE ts >= ?""", (since,)).fetchone()
    recent_sub_pct = (100.0 * win[0] / win[1]) if win[1] else 0.0
    return {
        "tokens": est["total"],
        "subagent_pct": recent_sub_pct,
        "subagent_pct_alltime": (100.0 * est["per_event_subagent"] / measured) if measured else 0,
        "cache_pct": (100.0 * (est["cache_read"] + est["cache_write"]) / measured) if measured else 0,
        "models": models,
        "vendors": vendors,
        "peak_sessions": sustained,
        "peak_day": peak_day,
        "window_days": window_days,
        "rank": rank,
        "tier": tier,
    }


def build(conn, rank: int | None = None, tier: str | None = None,
          window_days: int = 30) -> dict[str, dict]:
    m = collect_metrics(conn, rank, tier, window_days)
    out = {
        "tokens": _shield("tokens", _human(m["tokens"])),
        "subagents": _shield("subagent share", f"{m['subagent_pct']:.0f}%"),
        "swarm": _shield("swarm", f"{m['peak_sessions']} agents · {m['models']} models"),
        "peak": _shield(f"peak swarm ({window_days}d)", f"{m['peak_sessions']} concurrent agents"),
        "models": _shield("models", f"{m['models']} models · {m['vendors']} labs"),
        "cache": _shield("cache", f"{m['cache_pct']:.0f}% of tokens"),
    }
    if rank:
        out["viberank"] = _shield("viberank", f"#{rank}" + (f" · {tier}" if tier else ""))
    return out


def daily_series(conn, days: int = 30) -> list[tuple[str, int, int]]:
    """[(day, main_tokens, subagent_tokens)] for the last `days` active days."""
    tok = "input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens"
    rows = conn.execute(
        f"""SELECT substr(ts,1,10) d,
                   SUM(CASE WHEN is_sidechain=0 THEN {tok} ELSE 0 END),
                   SUM(CASE WHEN is_sidechain>0 THEN {tok} ELSE 0 END)
            FROM usage_events WHERE ts IS NOT NULL
            GROUP BY d ORDER BY d DESC LIMIT ?""", (days,)).fetchall()
    return [(d, m or 0, s or 0) for d, m, s in reversed(rows)]


def svg_chart(series: list[tuple[str, int, int]], width: int = 800,
              height: int = 200) -> str:
    """Stacked daily-token bars: main below, subagent above.

    Hand-rolled SVG rather than a plotting dependency — this has to render
    inside a GitHub README, where only static SVG survives.
    """
    if not series:
        return "<svg xmlns='http://www.w3.org/2000/svg'/>"
    pad_l, pad_b, pad_t = 52, 26, 16
    plot_w, plot_h = width - pad_l - 12, height - pad_b - pad_t
    peak = max((m + s) for _, m, s in series) or 1
    bar = plot_w / len(series)
    gap = min(2.0, bar * 0.18)

    def y(v: float) -> float:
        return pad_t + plot_h - (v / peak) * plot_h

    parts = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}' role='img' "
        f"aria-label='Daily token usage, main sessions and subagents'>",
        "<style>text{font:11px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "fill:#8b949e}.m{fill:#2f81f7}.s{fill:#d29922}</style>",
        f"<rect width='{width}' height='{height}' fill='#0d1117' rx='6'/>",
    ]
    for frac in (0.5, 1.0):
        gy = y(peak * frac)
        parts.append(f"<line x1='{pad_l}' y1='{gy:.1f}' x2='{width-12}' y2='{gy:.1f}' "
                     f"stroke='#21262d' stroke-width='1'/>")
        parts.append(f"<text x='{pad_l-6}' y='{gy+3:.1f}' text-anchor='end'>"
                     f"{_human(peak*frac)}</text>")
    for i, (day, main, subs) in enumerate(series):
        x = pad_l + i * bar
        w = max(bar - gap, 1)
        h_main = (main / peak) * plot_h
        h_sub = (subs / peak) * plot_h
        parts.append(f"<rect class='m' x='{x:.1f}' y='{y(main):.1f}' width='{w:.1f}' "
                     f"height='{h_main:.1f}'><title>{day}: {_human(main)} main</title></rect>")
        if subs:
            parts.append(f"<rect class='s' x='{x:.1f}' y='{y(main+subs):.1f}' width='{w:.1f}' "
                         f"height='{h_sub:.1f}'><title>{day}: {_human(subs)} subagent"
                         f"</title></rect>")
    first, last = series[0][0], series[-1][0]
    parts.append(f"<text x='{pad_l}' y='{height-6}'>{first}</text>")
    parts.append(f"<text x='{width-14}' y='{height-6}' text-anchor='end'>{last}</text>")
    parts.append(f"<rect class='m' x='{pad_l+120}' y='{height-14}' width='9' height='9'/>"
                 f"<text x='{pad_l+133}' y='{height-6}'>main</text>"
                 f"<rect class='s' x='{pad_l+180}' y='{height-14}' width='9' height='9'/>"
                 f"<text x='{pad_l+193}' y='{height-6}'>subagent</text>")
    parts.append("</svg>")
    return "".join(parts)


def run(out_dir: str | Path, db_path=None, rank: int | None = None,
        tier: str | None = None, days: int = 30) -> dict:
    conn = open_db(db_path)
    out = Path(out_dir).expanduser()
    (out / "badges").mkdir(parents=True, exist_ok=True)
    written = []
    for name, payload in build(conn, rank, tier, days).items():
        p = out / "badges" / f"{name}.json"
        p.write_text(json.dumps(payload))
        written.append(str(p))
    series = daily_series(conn, days)
    chart = out / "usage.svg"
    chart.write_text(svg_chart(series))
    written.append(str(chart))
    return {"written": written, "days_charted": len(series),
            "metrics": collect_metrics(conn, rank, tier, days)}
