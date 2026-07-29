#!/usr/bin/env bash
set -euo pipefail
cd "/Users/christianmc/projects/flightdeck"
python3 -m flightdeck.cli collect --quiet
