"""`flightdeck export --viberank` — ccusage-compatible leaderboard payload.

viberank (https://www.viberank.app, source: sculptdotfun/viberank) ingests
`ccusage --json`-shaped data: a `daily` array of per-date token/cost entries
plus a `totals` object. Its /api/submit route requires totals fields
inputTokens, outputTokens, cacheCreationTokens, cacheReadTokens, totalCost,
totalTokens, and a non-empty daily array; per-day `modelBreakdowns` are
accepted and sanitized field-by-field.

Flightdeck emits that exact shape from its own DB: EVERY provider (viberank
has an all-models view), all account roots merged, subagents included, grouped by UTC date. These are the dedup-corrected
numbers — subagent events counted exactly once — which makes the entry
defensible where naive scanners inflate their totals by double-counting
fan-out transcripts.

This module NEVER submits anything. It writes a file and prints the manual
submission paths. The only data in the payload: aggregate daily token counts,
model names, and computed USD cost. No prompts, no file paths, no project or
session names.
"""

from __future__ import annotations

import json
from pathlib import Path

from .db import event_cost_usd, load_pricing, open_db
from .paths import FLIGHTDECK_DIR

DEFAULT_OUT = FLIGHTDECK_DIR / "viberank-cc.json"

WHAT_LEAVES = (
    "aggregate daily token counts + model names + computed USD cost"
    " — no content, no paths, no project or session names"
)


def build(db_path=None, include_archive: bool = True) -> dict:
    conn = open_db(db_path)
    pricing = load_pricing(conn)
    cur = conn.execute(
        """
        SELECT substr(ts, 1, 10) AS day, model,
               SUM(input_tokens), SUM(output_tokens),
               SUM(cache_creation_tokens), SUM(cache_read_tokens),
               SUM(cache_5m_tokens), SUM(cache_1h_tokens)
        FROM usage_events
        WHERE ts IS NOT NULL AND length(ts) >= 10
        GROUP BY day, model
        ORDER BY day, model
        """
    )
    days: dict[str, dict] = {}
    for day, model, inp, out, cc, cr, c5, c1 in cur:
        model = model or "unknown"
        cost, _ = event_cost_usd(
            pricing,
            {
                "model": model, "cost_micros": None,
                "input_tokens": inp, "output_tokens": out,
                "cache_creation_tokens": cc, "cache_read_tokens": cr,
                "cache_5m_tokens": c5, "cache_1h_tokens": c1,
            },
        )
        d = days.setdefault(
            day,
            {
                "date": day,
                "inputTokens": 0, "outputTokens": 0,
                "cacheCreationTokens": 0, "cacheReadTokens": 0,
                "totalTokens": 0, "totalCost": 0.0,
                "modelsUsed": [], "modelBreakdowns": [],
            },
        )
        d["inputTokens"] += inp
        d["outputTokens"] += out
        d["cacheCreationTokens"] += cc
        d["cacheReadTokens"] += cr
        d["totalTokens"] += inp + out + cc + cr
        d["totalCost"] += cost
        d["modelsUsed"].append(model)
        d["modelBreakdowns"].append(
            {
                "modelName": model,
                "inputTokens": inp, "outputTokens": out,
                "cacheCreationTokens": cc, "cacheReadTokens": cr,
                "cost": round(cost, 6),
            }
        )
    if include_archive:
        _add_archive(conn, days, pricing)

    conn.close()

    daily = []
    totals = {
        "inputTokens": 0, "outputTokens": 0,
        "cacheCreationTokens": 0, "cacheReadTokens": 0,
        "totalTokens": 0, "totalCost": 0.0,
    }
    for day in sorted(days):
        d = days[day]
        d["modelsUsed"] = sorted(set(d["modelsUsed"]))
        d["totalCost"] = round(d["totalCost"], 6)
        daily.append(d)
        for k in ("inputTokens", "outputTokens", "cacheCreationTokens",
                  "cacheReadTokens", "totalTokens"):
            totals[k] += d[k]
        totals["totalCost"] += d["totalCost"]
    totals["totalCost"] = round(totals["totalCost"], 6)
    return {"daily": daily, "totals": totals}


def _add_archive(conn, days, pricing) -> None:
    """Fold in recovered sessions whose transcripts no longer exist.

    These are real, billed tokens — 27B of this estate — that per-event rows
    cannot represent because the transcripts were deleted before anything
    indexed them message-by-message. Two contributions, both additive and
    neither able to double-count:

      * sessions with NO per-event rows at all — added whole;
      * sessions where the archive saw MORE than the per-event data (it
        captured subagent transcripts the index missed) — only the positive
        delta is added.

    Precision caveat, stated plainly: an archive row is dated by its session,
    so a multi-day session lands on its start date. Token totals are right;
    day placement is coarser than the per-event portion.
    """
    tok = "input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens"
    per_event, per_event_day = {}, {}
    for sid, t, day in conn.execute(
        f"SELECT session_id, SUM({tok}), MIN(substr(ts,1,10)) FROM usage_events GROUP BY session_id"
    ):
        per_event[sid[:11]] = per_event.get(sid[:11], 0) + (t or 0)
        per_event_day.setdefault(sid[:11], day)

    agg: dict = {}
    try:
        rows = conn.execute(
            f"""SELECT source_file, day, model, input_tokens, output_tokens,
                       cache_creation_tokens, cache_read_tokens, cost_micros
                FROM archived_usage WHERE still_on_disk = 0"""
        ).fetchall()
    except Exception:
        return
    for source_file, day, model, inp, out, cc, cr, cost_micros in rows:
        sid = (source_file.split("mc-cache:", 1)[1] if source_file.startswith("mc-cache:")
               else Path(source_file).stem)[:11]
        e = agg.setdefault(sid, {"day": day, "model": model, "i": 0, "o": 0,
                                 "cc": 0, "cr": 0, "cost": None})
        e["i"] += inp or 0; e["o"] += out or 0
        e["cc"] += cc or 0; e["cr"] += cr or 0
        if cost_micros is not None:
            e["cost"] = (e["cost"] or 0) + cost_micros
        if not e["day"] and day:
            e["day"] = day
        if not e["model"] and model:
            e["model"] = model

    for sid, e in agg.items():
        total = e["i"] + e["o"] + e["cc"] + e["cr"]
        if not total:
            continue
        seen = per_event.get(sid, 0)
        if seen >= total:
            continue                      # per-event already covers it
        scale = (total - seen) / total    # add only the shortfall
        day = e["day"] or per_event_day.get(sid)
        if not day or len(day) < 10:
            continue
        model = e["model"] or "claude-unknown"
        inp, out = int(e["i"] * scale), int(e["o"] * scale)
        cc, cr = int(e["cc"] * scale), int(e["cr"] * scale)
        # A cost the archive recorded beats anything we can infer, and matters
        # most where `model` is NULL: the mb1 checkpoint has no model, so those
        # tokens would fall to a guessed tier that prices Feb–Mar 2026 cache
        # reads at the Opus 4.5+ rate when Opus 4.1 rates applied — 64% low
        # across 19.14B tokens. Scaled by the same shortfall factor as the
        # tokens so the two can never disagree.
        recorded = int(e["cost"] * scale) if e.get("cost") is not None else None
        cost, _ = event_cost_usd(pricing, {
            "model": model, "cost_micros": recorded,
            "input_tokens": inp, "output_tokens": out,
            "cache_creation_tokens": cc, "cache_read_tokens": cr,
            "cache_5m_tokens": 0, "cache_1h_tokens": 0})
        d = days.setdefault(day, {
            "date": day, "inputTokens": 0, "outputTokens": 0,
            "cacheCreationTokens": 0, "cacheReadTokens": 0,
            "totalTokens": 0, "totalCost": 0.0,
            "modelsUsed": [], "modelBreakdowns": []})
        d["inputTokens"] += inp; d["outputTokens"] += out
        d["cacheCreationTokens"] += cc; d["cacheReadTokens"] += cr
        d["totalTokens"] += inp + out + cc + cr
        d["totalCost"] += cost
        d["modelsUsed"].append(model)
        d["modelBreakdowns"].append({
            "modelName": model, "inputTokens": inp, "outputTokens": out,
            "cacheCreationTokens": cc, "cacheReadTokens": cr,
            "cost": round(cost, 6)})


def run(out: str | None = None) -> None:
    payload = build()
    if not payload["daily"]:
        raise SystemExit("no Claude usage rows in the DB — run `flightdeck collect` first")
    out_path = Path(out) if out else DEFAULT_OUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    t = payload["totals"]
    print(f"wrote {out_path}")
    print(
        f"  {len(payload['daily'])} days, {t['totalTokens']:,} tokens, "
        f"est ${t['totalCost']:,.2f} (dedup-corrected: subagent events counted once)"
    )
    print()
    print("flightdeck never submits for you. To put this on the board:")
    print("  a) sign in with GitHub at https://www.viberank.app and upload the file, or")
    print(f"  b) copy it to ./cc.json and run `npx viberank-cli` (it POSTs to"
          f" viberank.app/api/submit with your GitHub username)")
    print(f"data that leaves this machine if you do: {WHAT_LEAVES}")
