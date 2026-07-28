"""`flightdeck report` — windowed totals, human table or robot JSON.

Subagents are a first-class reporting dimension, not a footnote: the estate
runs as parallel sessions fanning out agents, so every section that carries
cost also carries the main/subagent split. Source classes come from the
is_sidechain column: 0 = main, 1 = plain subagent, 2 = workflow subagent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .db import event_cost_usd, load_pricing, open_db

WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}

TOKEN_COLS = [
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
    "cache_5m_tokens", "cache_1h_tokens", "reasoning_tokens",
]

SOURCE_NAMES = {0: "main", 1: "subagent", 2: "workflow-subagent"}


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def window_bounds(since: str, now: str | None):
    if since not in WINDOWS:
        raise SystemExit(f"--since must be one of {sorted(WINDOWS)}")
    end = (
        datetime.fromisoformat(now.replace("Z", "+00:00"))
        if now
        else datetime.now(timezone.utc)
    )
    start = end - WINDOWS[since]
    return iso_z(start), iso_z(end)


def _zero() -> dict:
    return {c: 0 for c in TOKEN_COLS} | {"events": 0, "cost_usd": 0.0, "cost_is_estimate": False}


def _add(agg: dict, row: dict, cost: float, est: bool) -> None:
    for c in TOKEN_COLS:
        agg[c] += row[c]
    agg["events"] += row["events"]
    agg["cost_usd"] += cost
    agg["cost_is_estimate"] = agg["cost_is_estimate"] or est


def _merge(agg: dict, other: dict) -> None:
    _add(agg, other, other["cost_usd"], other["cost_is_estimate"])


def _billed_tokens(v: dict) -> int:
    return sum(v[c] for c in TOKEN_COLS[:4])


def _with_share(v: dict, total_tokens: int, total_cost: float) -> dict:
    toks = _billed_tokens(v)
    return v | {
        "total_tokens": toks,
        "token_share": round(toks / total_tokens, 4) if total_tokens else 0.0,
        "cost_share": round(v["cost_usd"] / total_cost, 4) if total_cost else 0.0,
    }


def gather(since: str = "24h", now: str | None = None, db_path=None) -> dict:
    start, end = window_bounds(since, now)
    conn = open_db(db_path)
    pricing = load_pricing(conn)
    cur = conn.execute(
        f"""
        SELECT provider, account_root, model, is_sidechain,
               {', '.join(f'SUM({c})' for c in TOKEN_COLS)},
               COUNT(*), SUM(COALESCE(cost_micros, 0)),
               SUM(CASE WHEN cost_micros IS NOT NULL THEN 1 ELSE 0 END)
        FROM usage_events
        WHERE ts IS NOT NULL AND ts >= ? AND ts < ?
        GROUP BY provider, account_root, model, is_sidechain
        """,
        (start, end),
    )
    by_provider: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_account: dict[str, dict] = {}
    by_source: dict[str, dict] = {name: _zero() for name in SOURCE_NAMES.values()}
    spend_architecture: dict[str, dict] = {}
    by_model_source: dict[str, dict] = {}
    total = _zero()

    for r in cur:
        provider, account, model, side = r[0], r[1], r[2] or "?", r[3]
        source = SOURCE_NAMES.get(side, "subagent")
        row = dict(zip(TOKEN_COLS, r[4:11]))
        row = {k: int(v or 0) for k, v in row.items()}
        row["events"] = r[11]
        reported_micros, reported_n = r[12], r[13]
        if reported_n and reported_n == r[11]:
            cost, est = reported_micros / 1e6, False
        else:
            cost, est = event_cost_usd(
                pricing,
                row | {"model": model, "cost_micros": None},
            )
        _add(by_provider.setdefault(provider, _zero()), row, cost, est)
        _add(by_model.setdefault(model, _zero()), row, cost, est)
        _add(by_account.setdefault(f"{provider}/{account}", _zero()), row, cost, est)
        _add(by_source[source], row, cost, est)
        _add(spend_architecture.setdefault(f"{provider}/{source}", _zero()), row, cost, est)
        _add(by_model_source.setdefault(f"{model}/{source}", _zero()), row, cost, est)
        _add(total, row, cost, est)
    conn.close()

    hours = WINDOWS[since].total_seconds() / 3600
    total_tokens = _billed_tokens(total)
    total_cost = total["cost_usd"]
    grand = {
        **total,
        "total_tokens": total_tokens,
        "tokens_per_hour": round(total_tokens / hours, 1),
        "cost_per_day_usd": round(total_cost / (hours / 24), 4),
    }

    sub_all = _zero()
    _merge(sub_all, by_source["subagent"])
    _merge(sub_all, by_source["workflow-subagent"])
    sources = {
        "main": _with_share(by_source["main"], total_tokens, total_cost),
        "subagent_total": _with_share(sub_all, total_tokens, total_cost),
        "subagent_plain": _with_share(by_source["subagent"], total_tokens, total_cost),
        "workflow_subagent": _with_share(by_source["workflow-subagent"], total_tokens, total_cost),
    }

    return {
        "window": {"since": since, "start": start, "end": end},
        "totals": grand,
        "sources": sources,
        "spend_architecture": spend_architecture,
        "by_model_source": by_model_source,
        "by_provider": by_provider,
        "by_model": by_model,
        "by_account_root": by_account,
        "cache_split": {
            "input_tokens": total["input_tokens"],
            "output_tokens": total["output_tokens"],
            "cache_read_tokens": total["cache_read_tokens"],
            "cache_creation_tokens": total["cache_creation_tokens"],
            "cache_5m_tokens": total["cache_5m_tokens"],
            "cache_1h_tokens": total["cache_1h_tokens"],
        },
        # legacy key, kept for compatibility with early consumers
        "subagent": {
            "tokens": sources["subagent_total"]["total_tokens"],
            "share_of_total": sources["subagent_total"]["token_share"],
            "cost_usd": round(sources["subagent_total"]["cost_usd"], 4),
        },
    }


def _fmt(n: int | float) -> str:
    if isinstance(n, float):
        return f"{n:,.2f}"
    return f"{n:,}"


def _section_table(out: list[str], title: str, section: dict) -> None:
    out.append("")
    out.append(f"  {title}:")
    rows = sorted(section.items(), key=lambda kv: -_billed_tokens(kv[1]))
    name_w = max((len(k) for k, _ in rows), default=4) + 2
    out.append(
        f"    {'':<{name_w}}{'events':>9}{'in':>14}{'out':>12}{'cache_wr':>14}{'cache_rd':>16}{'cost$':>10}"
    )
    for name, v in rows:
        est = "*" if v["cost_is_estimate"] else ""
        out.append(
            f"    {name:<{name_w}}{_fmt(v['events']):>9}{_fmt(v['input_tokens']):>14}"
            f"{_fmt(v['output_tokens']):>12}{_fmt(v['cache_creation_tokens']):>14}"
            f"{_fmt(v['cache_read_tokens']):>16}{v['cost_usd']:>9,.2f}{est}"
        )


def render_human(data: dict) -> str:
    w = data["window"]
    t = data["totals"]
    out = []
    out.append(f"flightdeck report — last {w['since']}  ({w['start']} .. {w['end']})")
    out.append("")
    out.append(
        f"  total tokens: {_fmt(t['total_tokens'])}   events: {_fmt(t['events'])}   "
        f"est cost: ${t['cost_usd']:,.2f}{'*' if t['cost_is_estimate'] else ''}"
    )
    out.append(
        f"  burn: {_fmt(t['tokens_per_hour'])} tok/hr   ${t['cost_per_day_usd']:,.2f}/day"
    )
    cs = data["cache_split"]
    out.append(
        f"  cache: read {_fmt(cs['cache_read_tokens'])} | write {_fmt(cs['cache_creation_tokens'])} "
        f"(5m {_fmt(cs['cache_5m_tokens'])} / 1h {_fmt(cs['cache_1h_tokens'])}) | "
        f"uncached in {_fmt(cs['input_tokens'])} | out {_fmt(cs['output_tokens'])}"
    )

    s = data["sources"]
    out.append("")
    out.append("  main vs subagent:")
    for label, key in (("main", "main"), ("subagent", "subagent_total")):
        v = s[key]
        out.append(
            f"    {label:<11}{_fmt(v['total_tokens']):>16} tok ({v['token_share']:.1%})   "
            f"${v['cost_usd']:>10,.2f} ({v['cost_share']:.1%})   {_fmt(v['events'])} events"
        )
    out.append(
        f"    {'':<11}subagent = plain {_fmt(s['subagent_plain']['total_tokens'])}"
        f" + workflow {_fmt(s['workflow_subagent']['total_tokens'])} tok"
    )

    _section_table(out, "spend architecture (provider x source)", data["spend_architecture"])
    _section_table(out, "model x source", data["by_model_source"])
    _section_table(out, "by account root", data["by_account_root"])
    out.append("")
    out.append("  * = includes estimated pricing (see `pricing` table, is_estimate=1)")
    return "\n".join(out)


def run(since: str, as_json: bool, now: str | None = None) -> None:
    data = gather(since=since, now=now)
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        print(render_human(data))
