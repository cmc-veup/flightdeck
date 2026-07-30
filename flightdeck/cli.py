"""flightdeck command-line interface."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="flightdeck",
        description="Truthful multi-provider AI token-usage collector.",
    )
    p.add_argument("--version", action="version", version=f"flightdeck {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("collect", help="incremental scan of all sources into ~/.flightdeck/usage.db")
    pc.add_argument("--full", action="store_true", help="ignore the checkpoint and re-scan everything")
    pc.add_argument("--json", action="store_true", help="emit collection stats as JSON")
    pc.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")

    pr = sub.add_parser(
        "report",
        help="windowed usage totals with the main/subagent spend-architecture split",
    )
    pr.add_argument("--since", default="24h", choices=["24h", "7d", "30d"])
    pr.add_argument("--json", action="store_true", help="robot JSON output")
    pr.add_argument("--now", default=None, help=argparse.SUPPRESS)  # fixed window end (ISO Z), for verification

    pm = sub.add_parser("merge", help="merge another device's usage.db into this one")
    pm.add_argument("database", help="path to the other machine's ~/.flightdeck/usage.db")
    pm.add_argument("--device", default=None, help="label incoming rows as <device>:<root>")

    ps = sub.add_parser("submit", help="post the leaderboard payload (public, irreversible)")
    ps.add_argument("--viberank", action="store_true", help="submit to viberank.app")
    ps.add_argument("--user", default=None, help="your GitHub handle — required, never guessed")
    ps.add_argument("--payload", default=None, help="path to the export (default ~/.flightdeck/viberank-cc.json)")
    ps.add_argument("--yes", action="store_true", help="actually send; without it, prints what would be sent")

    pb = sub.add_parser("badges", help="shields.io endpoints + self-updating SVG chart for a profile README")
    pb.add_argument("--out", required=True, help="directory to write badges/ and usage.svg into")
    pb.add_argument("--rank", type=int, default=None, help="current viberank position")
    pb.add_argument("--tier", default=None, help="viberank tier label, e.g. Supernova")
    pb.add_argument("--rank-total", type=int, default=None,
                    help="board size, so the badge reads '#11 of ~1,000' instead of jargon")
    pb.add_argument("--days", type=int, default=30, help="days of history in the chart")

    sub.add_parser("total", help="the honest estate total (per-event + archive, max per session)")

    pd = sub.add_parser("doctor", help="data-source availability matrix")
    pd.add_argument("--json", action="store_true")

    sub.add_parser(
        "import-archive",
        help="recover usage from DELETED transcripts via ~/.claude/usage-checkpoint.json",
    )

    pe = sub.add_parser(
        "export",
        help="write a leaderboard payload (never auto-submits)",
    )
    pe.add_argument(
        "--viberank", action="store_true",
        help="ccusage-compatible JSON for viberank.app (Claude rows, dedup-corrected)",
    )
    pe.add_argument(
        "--rows", action="store_true",
        help="portable NDJSON of usage rows for multi-device sync via git (cwd redacted)")
    pe.add_argument("--include-cwd", action="store_true",
                    help="keep working directories in --rows (they name clients; private repos only)")
    pe.add_argument("--since", default=None, help="--rows: only events at/after this ISO date")
    pe.add_argument("--out", default=None, help="output file (default ~/.flightdeck/viberank-cc.json)")

    args = p.parse_args(argv)

    if args.cmd == "collect":
        from . import collect
        stats = collect.run(full=args.full, quiet=args.quiet or args.json)
        if args.json:
            print(json.dumps(stats, indent=2))
    elif args.cmd == "report":
        from . import report
        report.run(since=args.since, as_json=args.json, now=args.now)
    elif args.cmd == "merge":
        if str(args.database).endswith((".jsonl", ".ndjson")):
            from . import rows as _rows
            s = _rows.import_rows(args.database, device=args.device)
            s.setdefault("source", args.database)
        else:
            from . import merge
            s = merge.run(args.database, device=args.device)
        print(f"merged {s['source']}")
        print(f"  new events : {s['new_events']:,}  (duplicates ignored by primary key)")
        print(f"  new tokens : {s['new_tokens']:,}")
        print(f"  total now  : {s['total_events']:,} events / {s['total_tokens']:,} tokens")
    elif args.cmd == "submit":
        if not args.viberank:
            p.error("submit currently supports --viberank only")
        from . import submit
        from .paths import FLIGHTDECK_DIR
        payload = args.payload or (FLIGHTDECK_DIR / "viberank-cc.json")
        s = submit.run(payload, args.user, confirmed=args.yes)
        print(f"  payload : {s['payload']}")
        print(f"  content : {s['days']} days, {s['tokens']:,} tokens, ${s['cost']:,.2f}")
        print(f"  as user : {s['user']}")
        if not s["sent"]:
            print("\n  nothing sent. re-run with --yes to publish.")
            print("  what leaves this machine: daily token counts, model names, computed cost.")
            print("  no prompts, no paths, no project or session names.")
        else:
            r = s.get("response") or {}
            print(f"\n  submitted: {r.get('message') or r}")
            if r.get("profileUrl"):
                print(f"  profile  : {r['profileUrl']}")
    elif args.cmd == "badges":
        from . import badges
        r = badges.run(args.out, rank=args.rank, tier=args.tier, days=args.days,
                       rank_total=args.rank_total)
        m = r["metrics"]
        print(f"wrote {len(r['written'])} files to {args.out}")
        print(f"  tokens {m['tokens']:,} | subagent {m['subagent_pct']:.0f}%"
              f" | cache {m['cache_pct']:.0f}% | {m['models']} models / {m['vendors']} labs")
        print(f"  swarm  {m['peak_sessions']} sustained concurrent sessions on {m['peak_day']}")
        print(f"  chart  {r['days_charted']} days -> {args.out}/usage.svg")
    elif args.cmd == "total":
        from . import reconcile
        from .db import open_db
        print(reconcile.render(reconcile.totals(open_db())))
    elif args.cmd == "doctor":
        from . import doctor
        doctor.run(as_json=args.json)
    elif args.cmd == "import-archive":
        from . import archive
        total = 0
        s = archive.import_checkpoint()
        if s["found"]:
            print(f"usage-checkpoint: {s['rows']:,} file records")
            print(f"  recovered (deleted): {s['recovered_tokens']:,} tokens")
            total += s["recovered_tokens"]
        from . import agentsview_import
        av = agentsview_import.run()
        if av.get("found"):
            print(f"agentsview index: {av['new_events']:,} events recovered at FULL "
                  f"per-event fidelity ({av['already_present']:,} already present)")
            print(f"  recovered: {av['recovered_tokens']:,} tokens")
        m = archive.import_mission_control_cache()
        if m["found"]:
            print(f"mission-control cache: {m['rows']:,} sessions "
                  f"({m.get('skipped_already_counted', 0):,} already counted)")
            print(f"  recovered (deleted): {m['recovered_tokens']:,} tokens")
            total += m["recovered_tokens"]
        from .db import open_db
        conn = open_db()
        split = archive.apportion_models(conn)
        if split:
            blk = sum(split.values()) or 1
            print("  model mix applied from the same window's surviving rows: "
                  + ", ".join(f"{m.split('-2025')[0]} {100*v/blk:.0f}%"
                              for m, v in list(split.items())[:3]))
        t = archive.archive_totals(conn)
        print(f"ARCHIVE TOTAL: {t['tokens']:,} tokens across {t['files']:,} vanished transcripts")
    elif args.cmd == "export":
        if args.rows:
            from . import rows
            s = rows.export_rows(args.out or "flightdeck-rows.jsonl",
                                 include_cwd=args.include_cwd, since=args.since)
            print(f"wrote {s['out']}")
            print(f"  {s['events']:,} events / {s['tokens']:,} tokens"
                  f"  (cwd {'included' if s['cwd_included'] else 'REDACTED'})")
        elif args.viberank:
            from . import export_viberank
            export_viberank.run(out=args.out)
        else:
            p.error("export needs --viberank or --rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
