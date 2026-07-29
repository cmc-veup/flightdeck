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
        from . import merge
        s = merge.run(args.database, device=args.device)
        print(f"merged {s['source']}")
        print(f"  new events : {s['new_events']:,}  (duplicates ignored by primary key)")
        print(f"  new tokens : {s['new_tokens']:,}")
        print(f"  total now  : {s['total_events']:,} events / {s['total_tokens']:,} tokens")
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
        t = archive.archive_totals(open_db())
        print(f"ARCHIVE TOTAL: {t['tokens']:,} tokens across {t['files']:,} vanished transcripts")
    elif args.cmd == "export":
        if not args.viberank:
            p.error("export currently supports --viberank only")
        from . import export_viberank
        export_viberank.run(out=args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
