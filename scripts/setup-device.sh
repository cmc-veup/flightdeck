#!/usr/bin/env bash
# Set up flightdeck on one device. Idempotent — safe to re-run.
#
#   scripts/setup-device.sh                      # local only
#   scripts/setup-device.sh --sync-repo ~/usage  # also publish to a git repo
#
# Does four things, in order of how much they matter:
#   1. Stops Claude Code deleting your history (the urgent one).
#   2. First collect, so you can see numbers immediately.
#   3. Installs an hourly job: collect, and export to the sync repo if given.
#   4. Prints exactly what to run next.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYNC_REPO=""
DEVICE="$(hostname -s | tr '[:upper:]' '[:lower:]')"
while [ $# -gt 0 ]; do
  case "$1" in
    --sync-repo) SYNC_REPO="${2:?--sync-repo needs a path}"; shift 2 ;;
    --device)    DEVICE="${2:?--device needs a name}"; shift 2 ;;
    -h|--help)   sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

# ── 1. retention ─────────────────────────────────────────────────────────
# Claude Code deletes transcripts older than cleanupPeriodDays (default 30).
# Those transcripts are the only record of what you spent. This is the single
# highest-value line in the whole setup, and it is time-critical: every day it
# stays unset, another day of history ages toward deletion.
say "transcript retention"
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
  python3 - "$SETTINGS" <<'PY'
import json, sys, shutil, collections, datetime
p = sys.argv[1]
try:
    d = json.load(open(p), object_pairs_hook=collections.OrderedDict)
except Exception as e:
    print(f"    could not parse {p} ({e}) — set cleanupPeriodDays by hand"); raise SystemExit(0)
cur = d.get("cleanupPeriodDays")
if isinstance(cur, int) and cur >= 3650:
    print(f"    already {cur} days — nothing to do"); raise SystemExit(0)
shutil.copy(p, p + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
d["cleanupPeriodDays"] = 36500
json.dump(d, open(p, "w"), indent=2); open(p, "a").write("\n")
print(f"    was {cur!r} -> 36500 (backup written alongside)")
PY
else
  mkdir -p "$HOME/.claude"
  printf '{\n  "cleanupPeriodDays": 36500\n}\n' > "$SETTINGS"
  echo "    created $SETTINGS with cleanupPeriodDays=36500"
fi

# ── 2. first collect ─────────────────────────────────────────────────────
say "first collect (a large history takes a minute; later runs are incremental)"
cd "$REPO" && python3 -m flightdeck.cli collect

# ── 3. schedule ──────────────────────────────────────────────────────────
say "hourly collection"
RUNNER="$REPO/scripts/.device-tick.sh"
cat > "$RUNNER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$REPO"
python3 -m flightdeck.cli collect --quiet
EOF
if [ -n "$SYNC_REPO" ]; then
  mkdir -p "$SYNC_REPO/devices"
  cat >> "$RUNNER" <<EOF
python3 -m flightdeck.cli export --rows --out "$SYNC_REPO/devices/$DEVICE.jsonl" >/dev/null
cd "$SYNC_REPO"
git add "devices/$DEVICE.jsonl"
git diff --cached --quiet || git commit -qm "usage: $DEVICE \$(date -u +%Y-%m-%dT%H:%MZ)"
git pull --rebase -q --autostash 2>/dev/null || true
git push -q 2>/dev/null || echo "push failed (auth?) — rows are committed locally"
EOF
fi
chmod +x "$RUNNER"

if [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/com.flightdeck.device.plist"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.flightdeck.device</string>
  <key>ProgramArguments</key><array><string>$RUNNER</string></array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/.flightdeck/device.log</string>
  <key>StandardErrorPath</key><string>$HOME/.flightdeck/device.err.log</string>
</dict></plist>
EOF
  mkdir -p "$HOME/.flightdeck"
  launchctl bootout "gui/$(id -u)/com.flightdeck.device" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "    launchd agent com.flightdeck.device (hourly)"
else
  LINE="0 * * * * $RUNNER >> \$HOME/.flightdeck/device.log 2>&1"
  ( crontab -l 2>/dev/null | grep -v -F "$RUNNER" ; echo "$LINE" ) | crontab -
  echo "    crontab entry installed (hourly)"
fi

# ── 4. next steps ────────────────────────────────────────────────────────
say "done — device '$DEVICE'"
python3 -m flightdeck.cli total 2>/dev/null | head -3 || true
cat <<EOF

  see your numbers      flightdeck report --since 24h
  full estate total     flightdeck total
EOF
if [ -n "$SYNC_REPO" ]; then
cat <<EOF
  publishing to         $SYNC_REPO/devices/$DEVICE.jsonl  (hourly, cwd redacted)

  to combine every device, on any machine:
      cd "$SYNC_REPO" && git pull
      for f in devices/*.jsonl; do flightdeck merge "\$f"; done
      flightdeck total
EOF
else
cat <<EOF
  multiple devices?     re-run with --sync-repo ~/path/to/private/usage-repo
EOF
fi
