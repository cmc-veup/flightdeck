"""`flightdeck report` — windowed totals, human table or robot JSON."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .db import event_cost_usd, load_pricing, open_db

WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}

TOKEN_COLS = [
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
    "cache_5m_tokens", "cache_1h_tokens", "reasoning_tokens",
]


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
    total = _zero()
    sidechain = _zero()

    for r in cur:
        provider, account, model, is_side = r[0], r[1], r[2] or "?", r[3]
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
        _add(total, row, cost, est)
        if is_side:
            _add(sidechain, row, cost, est)
    conn.close()

    hours = WINDOWS[since].total_seconds() / 3600
    total_tokens = sum(total[c] for c in TOKEN_COLS[:4])
    grand = {
        **total,
        "total_tokens": total_tokens,
        "tokens_per_hour": round(total_tokens / hours, 1),
        "cost_per_day_usd": round(total["cost_usd"] / (hours / 24), 4),
    }
    side_tokens = sum(sidechain[c] for c in TOKEN_COLS[:4])
    return {
        "window": {"since": since, "start": start, "end": end},
        "totals": grand,
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
        "subagent": {
            "tokens": side_tokens,
            "share_of_total": round(side_tokens / total_tokens, 4) if total_tokens else 0.0,
            "cost_usd": round(sidechain["cost_usd"], 4),
        },
    }


def _fmt(n: int | float) -> str:
    if isinstance(n, float):
        return f"{n:,.2f}"
    return f"{n:,}"


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
    sa = data["subagent"]
    out.append(
        f"  subagents: {_fmt(sa['tokens'])} tokens ({sa['share_of_total']:.1%} of total)"
    )
    for title, section in (
        ("by provider", data["by_provider"]),
        ("by account root", data["by_account_root"]),
        ("by model", data["by_model"]),
    ):
        out.append("")
        out.append(f"  {title}:")
        rows = sorted(
            section.items(),
            key=lambda kv: -(kv[1]["input_tokens"] + kv[1]["output_tokens"]
                             + kv[1]["cache_read_tokens"] + kv[1]["cache_creation_tokens"]),
        )
        name_w = max((len(k) for k, _ in rows), default=4) + 2
        out.append(
            f"    {'':<{name_w}}{'in':>14}{'out':>12}{'cache_rd':>16}{'cache_wr':>14}{'cost$':>10}"
        )
        for name, v in rows:
            est = "*" if v["cost_is_estimate"] else ""
            out.append(
                f"    {name:<{name_w}}{_fmt(v['input_tokens']):>14}{_fmt(v['output_tokens']):>12}"
                f"{_fmt(v['cache_read_tokens']):>16}{_fmt(v['cache_creation_tokens']):>14}"
                f"{v['cost_usd']:>9,.2f}{est}"
            )
    out.append("")
    out.append("  * = includes estimated pricing (see `pricing` table, is_estimate=1)")
    return "\n".join(out)


def run(since: str, as_json: bool, now: str | None = None) -> None:
    data = gather(since=since, now=now)
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        print(render_human(data))
