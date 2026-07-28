#!/usr/bin/env python3
"""Independent recount of Claude-format usage for a time window.

Deliberately does NOT import the flightdeck collector: it re-implements the
summation naively from raw JSONL so it can cross-check the database. Dedupe is
by (sessionId, uuid); synthetic-model rows are skipped; journal.jsonl is
skipped; subagent files under subagents/ and subagents/workflows/wf_*/ are
included once.

Usage:
    python3 scripts/verify_recount.py --hours 24 --now 2026-07-28T15:00:00.000Z

Prints a JSON blob with per-column totals for Claude-format roots
(claude main/veup/pmme + deepseek + ollama). Compare against
`flightdeck report --json --since 24h --now <same value>`:
the claude+deepseek+ollama provider sums must match EXACTLY.
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("FLIGHTDECK_HOME_OVERRIDE", str(Path.home())))
ROOTS = {
    "claude": [
        HOME / ".claude" / "projects",
        HOME / ".claude-accounts" / "veup" / "projects",
        HOME / ".claude-accounts" / "pmme" / "projects",
    ],
    "deepseek": [HOME / ".claude-deepseek" / "projects"],
    "ollama": [HOME / ".claude-ollama" / "projects"],
}

COLS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens")


def iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--now", default=None, help="window end, ISO Z (default: now)")
    args = ap.parse_args()

    end = (datetime.fromisoformat(args.now.replace("Z", "+00:00"))
           if args.now else datetime.now(timezone.utc))
    start = end - timedelta(hours=args.hours)
    start_s, end_s = iso_z(start), iso_z(end)
    # only files touched since just before the window can hold in-window events
    mtime_floor = start.timestamp() - 3600

    result = {}
    for provider, roots in ROOTS.items():
        seen = set()
        totals = {c: 0 for c in COLS}
        events = 0
        sidechain_tokens = 0
        for root in roots:
            if not root.is_dir():
                continue
            for f in root.rglob("*.jsonl"):
                if f.name == "journal.jsonl":
                    continue
                try:
                    if os.stat(f).st_mtime < mtime_floor:
                        continue
                except OSError:
                    continue
                # parent-session fallback mirrors on-disk layout
                parts = f.parts
                if "subagents" in parts:
                    fb = parts[parts.index("subagents") - 1]
                    path_side = True
                else:
                    fb = f.stem
                    path_side = f.name.startswith("agent-")
                try:
                    fh = open(f, "r", encoding="utf-8", errors="replace")
                except OSError:
                    continue
                with fh:
                    for line in fh:
                        if '"usage"' not in line:
                            continue
                        try:
                            o = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(o, dict) or o.get("type") != "assistant":
                            continue
                        msg = o.get("message")
                        if not isinstance(msg, dict):
                            continue
                        u = msg.get("usage")
                        if not isinstance(u, dict):
                            continue
                        if (msg.get("model") or "") == "<synthetic>":
                            continue
                        ts = o.get("timestamp")
                        if not ts or not (start_s <= ts < end_s):
                            continue
                        uuid = o.get("uuid")
                        if not uuid:
                            continue
                        key = (o.get("sessionId") or fb, uuid)
                        if key in seen:
                            continue
                        seen.add(key)
                        events += 1
                        row_total = 0
                        for c in COLS:
                            v = int(u.get(c) or 0)
                            totals[c] += v
                            row_total += v
                        if o.get("isSidechain") or path_side:
                            sidechain_tokens += row_total
        result[provider] = {
            **totals,
            "events": events,
            "total_tokens": sum(totals.values()),
            "sidechain_tokens": sidechain_tokens,
        }

    print(json.dumps({"window": {"start": start_s, "end": end_s}, "providers": result},
                     indent=2))


if __name__ == "__main__":
    main()
