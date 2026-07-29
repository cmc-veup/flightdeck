"""The honest estate total, reconciling per-event data against the archive.

Two sources describe overlapping sessions and NEITHER is a superset:

* per-event rows (`usage_events`) — precise, but only for transcripts that
  survived on disk or were recovered from an index. The agentsview index
  barely captured subagents (475 subagent sessions of 74,923), so recovered
  months are missing most fan-out.
* archive rows (`archived_usage`) — one row per transcript FILE, including
  every `agent-*.jsonl`, from tools that scanned before the deletions. Coarse
  (session-dated aggregates) but it SAW the subagent transcripts.

So "per-event always wins" is wrong: it silently discarded 11.78B of real
subagent burn on this machine. Subagent tokens are not double-counting —
they are billed API calls, a third to a half of the estate's spend.

The rule is MAX PER SESSION. Both sources are floors (each can only miss
data, never invent it), so the larger figure is the better floor. Identical
sessions collapse instead of summing, and a session the other source never
saw is added whole.
"""

from __future__ import annotations

import collections
from pathlib import Path

KEY = 11  # mission-control truncates session ids to 11 chars


def _session_key(source_file: str) -> str:
    if source_file.startswith("mc-cache:"):
        return source_file.split("mc-cache:", 1)[1][:KEY]
    return Path(source_file).stem[:KEY]


def totals(conn) -> dict:
    tok = ("input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens")

    per_event = collections.Counter()
    for sid, t in conn.execute(
        f"SELECT session_id, SUM({tok}) FROM usage_events GROUP BY session_id"
    ):
        per_event[sid[:KEY]] += t or 0

    archive = collections.Counter()
    try:
        rows = conn.execute(
            f"SELECT source_file, {tok} FROM archived_usage WHERE still_on_disk = 0"
        )
    except Exception:
        rows = []
    for source_file, t in rows:
        archive[_session_key(source_file)] += t or 0

    both = set(per_event) & set(archive)
    only_pe = sum(per_event[s] for s in set(per_event) - both)
    only_ar = sum(archive[s] for s in set(archive) - both)
    overlap = sum(max(per_event[s], archive[s]) for s in both)

    breakdown = conn.execute(
        """SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                  COALESCE(SUM(cache_creation_tokens),0), COALESCE(SUM(cache_read_tokens),0)
           FROM usage_events"""
    ).fetchone()
    sub = conn.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN is_sidechain>0 THEN {tok} ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN is_sidechain=0 THEN {tok} ELSE 0 END),0)
            FROM usage_events"""
    ).fetchone()

    return {
        "total": only_pe + only_ar + overlap,
        "per_event_only": only_pe,
        "archive_only": only_ar,
        "overlap_max": overlap,
        "sessions_both": len(both),
        "archive_wins": sum(1 for s in both if archive[s] > per_event[s]),
        "uncached_input": breakdown[0], "output": breakdown[1],
        "cache_write": breakdown[2], "cache_read": breakdown[3],
        "per_event_subagent": sub[0], "per_event_main": sub[1],
    }


def render(t: dict) -> str:
    total = t["total"]
    cache = t["cache_read"] + t["cache_write"]
    measured = t["per_event_main"] + t["per_event_subagent"]
    pct = (100.0 * t["per_event_subagent"] / measured) if measured else 0
    return "\n".join([
        f"  ESTATE TOTAL: {total:,} tokens",
        "",
        f"    per-event only sessions : {t['per_event_only']:>16,}",
        f"    archive only sessions   : {t['archive_only']:>16,}",
        f"    overlapping (max/session): {t['overlap_max']:>16,}"
        f"   [{t['sessions_both']:,} sessions; archive larger for {t['archive_wins']:,}]",
        "",
        "  composition of the per-event portion (cache IS counted):",
        f"    cache read              : {t['cache_read']:>16,}",
        f"    cache write             : {t['cache_write']:>16,}",
        f"    uncached input          : {t['uncached_input']:>16,}",
        f"    output                  : {t['output']:>16,}",
        f"    -> cache is {100.0*cache/measured:.1f}% of measured tokens" if measured else "",
        "",
        f"    main sessions           : {t['per_event_main']:>16,}",
        f"    subagents (real burn)   : {t['per_event_subagent']:>16,}  ({pct:.1f}%)",
    ])
