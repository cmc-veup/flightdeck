#!/usr/bin/env bash
set -euo pipefail
cd "/Users/christianmc/projects/flightdeck"

# `timeout` is NOT in macOS base — it is Homebrew coreutils — and launchd runs
# with a minimal PATH that excludes /opt/homebrew/bin. Calling it bare made this
# job exit 127 (command not found) every hour: collection silently stopped, and
# only doctor's collector_scheduled gate caught it. Resolve it explicitly, and
# fall back to running unguarded rather than not running at all — a job with no
# ceiling still collects; a job that cannot start collects nothing.
TIMEOUT=""
for c in /opt/homebrew/bin/timeout /usr/local/bin/timeout /usr/bin/timeout; do
  [ -x "$c" ] && { TIMEOUT="$c"; break; }
done

if [ -n "$TIMEOUT" ]; then
  "$TIMEOUT" 900 python3 -m flightdeck.cli collect --quiet
else
  echo "device-tick: no timeout(1) found — running unguarded" >&2
  python3 -m flightdeck.cli collect --quiet
fi
