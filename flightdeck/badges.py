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
from .db import load_pricing, match_price
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


def swarm_day(conn, day: str) -> dict:
    """Characterise one day's swarm as a FLOW, not a snapshot.

    Peak-concurrency-in-a-bucket is the wrong instrument on its own. A swarm
    scales to a wave of work, runs it, contracts, and scales again — that
    breathing IS the operating pattern, and an agent that finishes its task in
    twelve minutes is a good agent. A point-in-time maximum penalises exactly
    the behaviour you want and hides the throughput.

    So report three things together:
      agents   - distinct sustained sessions that ran that day (throughput)
      peak     - most concurrent in any 10-minute bucket (amplitude)
      held_*   - minutes spent at or above a threshold (endurance)

    `held` is what separates a spike from a wave. 2026-07-30 peaked at 115 but
    held 100+ for an hour and 50+ for 100 minutes; calling that a burst would
    be as wrong as calling a single bucket sustained.
    """
    rows = conn.execute(
        "SELECT session_id, ts FROM usage_events WHERE ts LIKE ?", (day + "%",)).fetchall()
    if not rows:
        return {"agents": 0, "peak": 0, "held_50": 0, "held_100": 0}
    per = collections.Counter(s for s, _ in rows)
    real = {s for s, n in per.items() if n >= SUSTAINED_EVENTS}
    buckets: dict[str, set] = collections.defaultdict(set)
    for sid, ts in rows:
        if sid in real:
            buckets[ts[:15]].add(sid)          # ts[:15] == a 10-minute bucket
    sizes = sorted((len(v) for v in buckets.values()), reverse=True)
    return {
        "agents": len(real),
        "peak": sizes[0] if sizes else 0,
        "held_50": sum(1 for x in sizes if x >= 50) * 10,
        "held_100": sum(1 for x in sizes if x >= 100) * 10,
    }


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
    pricing_rows = load_pricing(conn)
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
    # Rank wave days by AGENTS RUN, not by instantaneous peak. A day that put
    # 503 agents through the queue is a bigger swarm day than one that briefly
    # touched a higher number, and it is the figure that survives the
    # scale-up/contract cycle.
    # Two DIFFERENT days, and conflating them understates. The day that ran the
    # most agents is not necessarily the day that ran the most at once: 2026-06-04
    # put 620 agents through the queue but peaked at 26 concurrent, while
    # 2026-07-28 peaked at 123 off 316 agents. Track each separately.
    peak_day, sustained, best, sustained_day = None, 0, {}, None
    for (d,) in conn.execute(
            "SELECT DISTINCT substr(ts,1,10) FROM usage_events"
            " WHERE ts >= ? ORDER BY 1", (since,)):
        s = swarm_day(conn, d)
        if s["agents"] > (best.get("agents") or 0):
            best, peak_day = s, d
        if s["peak"] > sustained:
            sustained, sustained_day = s["peak"], d
    # The elastic RANGE is the story, not a single maximum. Between waves the
    # swarm sits at zero — nothing idles, nothing is paid for. During one it
    # runs in the hundreds. Quote both ends.
    wave_sizes = [s["agents"] for (d,) in conn.execute(
        "SELECT DISTINCT substr(ts,1,10) FROM usage_events WHERE ts >= ?", (since,))
        if (s := swarm_day(conn, d))["peak"] >= 50]
    wave_days = len(wave_sizes)
    wave_min = min(wave_sizes) if wave_sizes else 0
    wave_max = max(wave_sizes) if wave_sizes else 0
    # Ceiling is all-time, not windowed: it is a capability, and capability does
    # not expire because last month was quiet. Typical range stays windowed,
    # because that IS what it currently does.
    wave_ceiling = wave_max
    for (d,) in conn.execute(
            "SELECT DISTINCT substr(ts,1,10) FROM usage_events WHERE ts IS NOT NULL"):
        n = swarm_day(conn, d)["agents"]
        if n > wave_ceiling:
            wave_ceiling = n
    win = conn.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN is_sidechain>0 THEN {tok} ELSE 0 END),0),
                   COALESCE(SUM({tok}),0)
            FROM usage_events WHERE ts >= ?""", (since,)).fetchone()
    recent_sub_pct = (100.0 * win[0] / win[1]) if win[1] else 0.0
    # All-time subagent share reports DELETION DAMAGE, not fan-out. Months whose
    # subagent transcripts were deleted before indexing read 0-24%; intact ones
    # read 27-48%. Averaging them together publishes the damage as behaviour, so
    # compute the share over capture-complete months only, archive included.
    atok = ("COALESCE(input_tokens,0)+COALESCE(output_tokens,0)"
            "+COALESCE(cache_creation_tokens,0)+COALESCE(cache_read_tokens,0)")
    per_month: dict[str, list[int]] = {}
    for mo, t, s in conn.execute(
            f"""SELECT substr(ts,1,7), SUM({tok}),
                       SUM(CASE WHEN is_sidechain>0 THEN {tok} ELSE 0 END)
                  FROM usage_events GROUP BY 1"""):
        per_month.setdefault(mo, [0, 0])
        per_month[mo][0] += t or 0
        per_month[mo][1] += s or 0
    try:
        for mo, t, s in conn.execute(
                f"""SELECT substr(day,1,7), SUM({atok}),
                           SUM(CASE WHEN is_sidechain>0 THEN {atok} ELSE 0 END)
                      FROM archived_usage
                     WHERE still_on_disk=0 AND day IS NOT NULL GROUP BY 1"""):
            if mo:
                per_month.setdefault(mo, [0, 0])
                per_month[mo][0] += t or 0
                per_month[mo][1] += s or 0
    except Exception:
        pass
    SUSPECT_BELOW = 25.0          # intact months never fall below ~27%
    it = isub = 0
    intact_months = damaged_months = 0
    for mo, (t, s) in per_month.items():
        if not t:
            continue
        if 100.0 * s / t >= SUSPECT_BELOW:
            it += t
            isub += s
            intact_months += 1
        else:
            damaged_months += 1
    subagent_pct_intact = (100.0 * isub / it) if it else 0.0

    # ACTIVE days, not calendar days — 48 days in the span have no rows at all.
    # Callers must say "active", and must alert if this figure ever FALLS:
    # by this project's own reasoning a decreasing total is proof of deletion.
    days_active = conn.execute(
        "SELECT COUNT(DISTINCT substr(ts,1,10)) FROM usage_events").fetchone()[0]
    span = conn.execute(
        "SELECT MIN(substr(ts,1,10)), MAX(substr(ts,1,10)) FROM usage_events").fetchone()

    # How much of the estate is priced from a real rate card. The README used
    # to claim this existed; it did not. An unmatched model returns $0 and
    # would otherwise masquerade as thrift.
    unpriced = estimated = priced = 0
    for m, t, ts in conn.execute(
            f"""SELECT model, SUM({tok}), MAX(ts) FROM usage_events
                 WHERE model IS NOT NULL AND model <> '<synthetic>'
                 GROUP BY 1"""):
        p = match_price(pricing_rows, m, ts)
        if p is None:
            unpriced += t or 0
        elif p[5]:
            estimated += t or 0
        else:
            priced += t or 0
    priced_total = unpriced + estimated + priced

    # Concentration matters more than the model count: 24 models across 9 labs
    # is only diversity if the spend is spread.
    top_vendor_pct = 0.0
    vend: dict[str, int] = {}
    for m, t in conn.execute(
            f"""SELECT model, SUM({tok}) FROM usage_events
                 WHERE model IS NOT NULL GROUP BY 1"""):
        vend[vendor_of(m)] = vend.get(vendor_of(m), 0) + (t or 0)
    if vend:
        top_vendor_pct = 100.0 * max(vend.values()) / max(sum(vend.values()), 1)

    return {
        "tokens": est["total"],
        "days_active": days_active,
        "days_covered": days_active,          # legacy alias
        "span_start": span[0], "span_end": span[1],
        "subagent_pct": recent_sub_pct,
        "subagent_pct_alltime": (100.0 * est["per_event_subagent"] / measured) if measured else 0,
        # The defensible all-time figure: damaged months excluded, archive in.
        "subagent_pct_intact": subagent_pct_intact,
        "intact_months": intact_months,
        "damaged_months": damaged_months,
        # Reads only. Cache WRITES are not "served from" cache and cost a
        # premium, so folding them in overstates the saving.
        "cache_read_pct": (100.0 * est["cache_read"] / measured) if measured else 0,
        "cache_pct": (100.0 * (est["cache_read"] + est["cache_write"]) / measured) if measured else 0,
        "unpriced_pct": (100.0 * unpriced / priced_total) if priced_total else 0,
        "estimated_pct": (100.0 * estimated / priced_total) if priced_total else 0,
        "top_vendor_pct": top_vendor_pct,
        "models": models,
        "vendors": vendors,
        "peak_sessions": sustained,
        "peak_day": peak_day,
        "swarm_agents": best.get("agents", 0),
        "swarm_peak": sustained,
        "swarm_peak_day": sustained_day,
        "swarm_held_50": best.get("held_50", 0),
        "swarm_held_100": best.get("held_100", 0),
        "wave_days": wave_days,
        "wave_min": wave_min,
        "wave_ceiling": wave_ceiling,
        "wave_max": wave_max,
        "window_days": window_days,
        "rank": rank,
        "tier": tier,
    }


def build(conn, rank: int | None = None, tier: str | None = None,
          window_days: int = 30, rank_total: int | None = None) -> dict[str, dict]:
    m = collect_metrics(conn, rank, tier, window_days)
    out = {
        "tokens": _shield("tokens", _human(m["tokens"])),
        "subagents": _shield("subagent share", f"{m['subagent_pct']:.0f}%"),
        # Agents RUN leads; peak concurrency is the amplitude of the wave, not
        # its size. A swarm that scales and contracts through 503 agents is
        # bigger than one that briefly touched a higher instantaneous number.
        "swarm": _shield("swarm", f"{m['swarm_agents']} agents · {m['swarm_peak']} concurrent"),
        "peak": _shield("peak wave", f"{m['swarm_peak']} concurrent held {m['swarm_held_100'] or m['swarm_held_50']}min"),
        "models": _shield("models", f"{m['models']} models · {m['vendors']} labs"),
        "cache": _shield("cache", f"{m['cache_pct']:.0f}% of tokens"),
    }
    if rank:
        msg = f"#{rank} · {tier}" if tier else f"#{rank}"
        out["viberank"] = _shield("viberank", msg)
    return out


def daily_series(conn, days: int = 30) -> list[tuple[str, int, int]]:
    """[(day, main_tokens, subagent_tokens)] for the last `days` active days.

    Includes recovered archive rows, not just per-event. Reading usage_events
    alone undercounts Feb-Jun by 47-70%, because those months' transcripts were
    deleted and only survive in the archives — the estate TOTAL already folds
    them in, so a per-event-only chart silently disagrees with the headline
    number it sits under.

    Archive rows are dated by session start, so their day placement is coarser
    than per-event. That is a real limitation, and it is still far better than
    omitting half of a month.
    """
    tok = "input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens"
    atok = ("COALESCE(input_tokens,0)+COALESCE(output_tokens,0)"
            "+COALESCE(cache_creation_tokens,0)+COALESCE(cache_read_tokens,0)")
    agg: dict[str, list[int]] = {}
    for d, m, s in conn.execute(
        f"""SELECT substr(ts,1,10) d,
                   SUM(CASE WHEN is_sidechain=0 THEN {tok} ELSE 0 END),
                   SUM(CASE WHEN is_sidechain>0 THEN {tok} ELSE 0 END)
            FROM usage_events WHERE ts IS NOT NULL GROUP BY d"""):
        agg[d] = [m or 0, s or 0]
    try:
        for d, m, s in conn.execute(
            f"""SELECT day,
                       SUM(CASE WHEN is_sidechain=0 THEN {atok} ELSE 0 END),
                       SUM(CASE WHEN is_sidechain>0 THEN {atok} ELSE 0 END)
                FROM archived_usage
                WHERE still_on_disk=0 AND day IS NOT NULL AND length(day)=10
                GROUP BY day"""):
            e = agg.setdefault(d, [0, 0])
            e[0] += m or 0
            e[1] += s or 0
    except Exception:
        pass                       # archive table absent on a fresh install
    return [(d, *agg[d]) for d in sorted(agg)[-days:]]


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
        tier: str | None = None, days: int = 30,
        rank_total: int | None = None) -> dict:
    conn = open_db(db_path)
    out = Path(out_dir).expanduser()
    (out / "badges").mkdir(parents=True, exist_ok=True)
    written = []
    for name, payload in build(conn, rank, tier, days, rank_total).items():
        p = out / "badges" / f"{name}.json"
        p.write_text(json.dumps(payload))
        written.append(str(p))
    series = daily_series(conn, days)
    chart = out / "usage.svg"
    chart.write_text(svg_chart(series))
    written.append(str(chart))
    # Machine-readable metrics, so prose that quotes these numbers can be
    # regenerated rather than retyped. A README claiming to be live while
    # carrying hand-typed figures is exactly the drift this tool exists to
    # catch.
    mfile = out / "badges" / "metrics.json"
    mfile.write_text(json.dumps(collect_metrics(conn, rank, tier, days), indent=2))
    written.append(str(mfile))
    return {"written": written, "days_charted": len(series),
            "metrics": collect_metrics(conn, rank, tier, days)}
