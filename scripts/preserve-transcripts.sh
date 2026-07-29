#!/usr/bin/env bash
# Append-only mirror of every agent transcript on this machine.
#
# Claude Code deletes transcripts on its own schedule, and disk-pressure
# cleanups have taken more — April and May 2026 are already gone. The
# flightdeck DB captures the NUMBERS hourly, but the transcripts themselves
# (prompts, decisions, tool calls) are irreplaceable once removed.
#
# This mirror never deletes: rsync runs WITHOUT --delete, so a file that
# vanishes from the source stays in the archive forever. Re-running is cheap
# (rsync skips unchanged files) and safe (nothing is ever overwritten with
# less data — --update keeps the newer copy).
#
# Deliberately NOT inside a Syncthing folder: the archive must not be
# replicated into a tree whose deletions propagate.
set -euo pipefail

ARCHIVE="${TRANSCRIPT_ARCHIVE:-$HOME/transcript-archive}"
mkdir -p "$ARCHIVE"

mirror() {
  local src="$1" name="$2"
  [ -d "$src" ] || return 0
  mkdir -p "$ARCHIVE/$name"
  # -a archive, --update keep-newer, no --delete: append-only by construction
  rsync -a --update --prune-empty-dirs \
        --include='*/' --include='*.jsonl' --include='*.json' --exclude='*' \
        "$src/" "$ARCHIVE/$name/"
}

# Claude Code roots (main + every account / provider profile), discovered.
mirror "$HOME/.claude/projects" "claude-main"
for d in "$HOME"/.claude-accounts/*/projects; do
  [ -d "$d" ] || continue
  mirror "$d" "claude-account-$(basename "$(dirname "$d")")"
done
for d in "$HOME"/.claude-*/projects; do
  [ -d "$d" ] || continue
  parent="$(basename "$(dirname "$d")")"
  [ "$parent" = ".claude-accounts" ] && continue
  mirror "$d" "claude-${parent#.claude-}"
done

# Syncthing-replicated session archives (transcripts from the other machine).
mirror "$HOME/.session-vc" "session-vc"
mirror "$HOME/.session-gt" "session-gt"

# Other agent CLIs that keep their own transcript stores.
mirror "$HOME/.codex/sessions" "codex-sessions"
mirror "$HOME/.gemini/tmp" "gemini-tmp"
mirror "$HOME/.gemini/history" "gemini-history"
mirror "$HOME/.qwen" "qwen"
mirror "$HOME/.openclaw" "openclaw"
mirror "$HOME/.gt" "gastown"
mirror "$HOME/.vc-caches" "vc-caches"

# SQLite stores — copied via `.backup` so an in-flight write can't hand us a
# torn file (a plain cp of a live DB is how you archive corruption).
snapshot_db() {
  local db="$1" name="$2"
  [ -f "$db" ] || return 0
  mkdir -p "$ARCHIVE/_databases"
  sqlite3 "$db" ".backup '$ARCHIVE/_databases/$name'" 2>/dev/null \
    || cp "$db" "$ARCHIVE/_databases/$name" 2>/dev/null || true
}
snapshot_db "$HOME/.grok/grok.db"              "grok.db"
snapshot_db "$HOME/.agentsview/sessions.db"    "agentsview-sessions.db"
snapshot_db "$HOME/.codex/logs_2.sqlite"       "codex-logs.sqlite"
snapshot_db "$HOME/.codex/state_5.sqlite"      "codex-state.sqlite"
snapshot_db "$HOME/.codex/memories_1.sqlite"   "codex-memories.sqlite"
snapshot_db "$HOME/.flightdeck/usage.db"       "flightdeck-usage.db"

# Small but irreplaceable aggregate archives — the only record of eras whose
# transcripts are already gone.
mkdir -p "$ARCHIVE/_aggregates"
for f in "$HOME/.claude/usage-checkpoint.json" "$HOME/vc/.usage-cache.json" \
         "$HOME/vc/.usage-history.jsonl" "$HOME/.gt/costs.jsonl"; do
  [ -f "$f" ] && rsync -a --update "$f" "$ARCHIVE/_aggregates/" || true
done

printf 'transcript archive: %s (%s)\n' "$ARCHIVE" "$(du -sh "$ARCHIVE" 2>/dev/null | cut -f1)"
