#!/usr/bin/env bash
set -euo pipefail
cd "/Users/christianmc/projects/flightdeck"
# Ceiling, for the same reason refresh.sh has one: launchd starts the next copy
# on schedule and never notices the last one never returned. collect is
# incremental, so a killed run costs nothing -- the next pass rescans the same
# files. A hang with no ceiling costs every subsequent run.
timeout 900 python3 -m flightdeck.cli collect --quiet
