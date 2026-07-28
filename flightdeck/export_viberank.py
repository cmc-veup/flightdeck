"""`flightdeck export --viberank` — ccusage-compatible leaderboard payload.

viberank (https://www.viberank.app, source: sculptdotfun/viberank) ingests
`ccusage --json`-shaped data: a `daily` array of per-date token/cost entries
plus a `totals` object. Its /api/submit route requires totals fields
inputTokens, outputTokens, cacheCreationTokens, cacheReadTokens, totalCost,
totalTokens, and a non-empty daily array; per-day `modelBreakdowns` are
accepted and sanitized field-by-field.

Flightdeck emits that exact shape from its own DB: Claude-provider rows only,
all account roots merged, grouped by UTC date. These are the dedup-corrected
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


def build(db_path=None) -> dict:
    conn = open_db(db_path)
    pricing = load_pricing(conn)
    cur = conn.execute(
        """
        SELECT substr(ts, 1, 10) AS day, model,
               SUM(input_tokens), SUM(output_tokens),
               SUM(cache_creation_tokens), SUM(cache_read_tokens),
               SUM(cache_5m_tokens), SUM(cache_1h_tokens)
        FROM usage_events
        WHERE provider = 'claude' AND ts IS NOT NULL AND length(ts) >= 10
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
