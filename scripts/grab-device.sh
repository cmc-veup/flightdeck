#!/usr/bin/env bash
# Pull EVERY agent transcript and usage store off mb1 into the archive.
#
#   ./scripts/grab-device.sh <mb1-hostname-or-ip> [remote-user]
#   ./scripts/grab-device.sh mb1.local
#
# Prereq on mb1: System Settings → General → Sharing → Remote Login (SSH) on.
#
# ############################################################################
# DO NOT LAUNCH CLAUDE CODE ON THE SOURCE MACHINE.
#
# Claude Code runs its retention sweep at STARTUP and deletes transcripts older
# than cleanupPeriodDays (default 30). The transcripts worth crossing the room
# for are months old, so opening Claude Code on mb1 destroys the exact thing
# this script exists to recover — before you ever get to run it.
#
# If you must use mb1 afterwards, EDIT ~/.claude/settings.json there FIRST
# (cleanupPeriodDays: 36500), with a text editor, without launching the app.
# The preflight below reports mb1's current setting so you know what you are
# walking into. Codex has no equivalent sweep — its rollouts are safe.
# ############################################################################
#
# Safety: everything lands under <archive>/devices/<user>/ — a SEPARATE tree,
# and the ONLY location flightdeck will ingest as a remote device (scanning the
# archive root re-ingests this machine's own mirror under a different provider
# label and double-counts it — ALARM-4). Nothing local is overwritten, rsync
# runs without --delete, and identical events collide on the
# (provider, session_id, event_id) primary key — so this can be re-run any
# number of times and can only ever ADD data.
set -uo pipefail

MB1="${1:-}"
REMOTE_USER="${2:-mchack}"
ARCHIVE="${TRANSCRIPT_ARCHIVE:-$HOME/transcript-archive}/devices/${2:-mb1}"

if [ -z "$MB1" ]; then
  echo "usage: $0 <mb1-hostname-or-ip> [remote-user]   (default user: mchack)" >&2
  exit 2
fi

echo "==> preflight: ssh ${REMOTE_USER}@${MB1}"
if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "${REMOTE_USER}@${MB1}" "echo ok" >/dev/null 2>&1; then
  echo "   cannot SSH non-interactively to ${REMOTE_USER}@${MB1}" >&2
  echo "   on mb1: System Settings > General > Sharing > Remote Login" >&2
  echo "   then:   ssh-copy-id ${REMOTE_USER}@${MB1}   (or re-run and type the password)" >&2
  echo "   retrying interactively..." >&2
  ssh -o ConnectTimeout=8 "${REMOTE_USER}@${MB1}" "echo ok" || exit 1
fi

echo "==> remote inventory"
ssh "${REMOTE_USER}@${MB1}" 'bash -s' <<'REMOTE'
  for d in ~/.claude/projects ~/.claude-accounts ~/.codex/sessions ~/.grok ~/.gemini ~/.qwen ~/.session-vc ~/.session-gt; do
    [ -e "$d" ] && printf "   %-28s %8s  %s jsonl\n" "$(basename $d)" \
      "$(du -sh $d 2>/dev/null | cut -f1)" \
      "$(find $d -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')"
  done

  # Answer the only question that decides whether this trip was worth taking,
  # BEFORE any bytes move: did the old eras survive, and is a sweep armed?
  echo
  echo "   --- survival check ---"
  cp=$(grep -o '"cleanupPeriodDays"[^,}]*' ~/.claude/settings.json 2>/dev/null | head -1)
  echo "   retention        ${cp:-UNSET -> Claude Code defaults to 30 days}"
  if [ -d ~/.claude/projects ]; then
    printf "   oldest transcript %s\n" \
      "$(find ~/.claude/projects -name '*.jsonl' -exec stat -f '%Sm' -t '%Y-%m-%d' {} \; 2>/dev/null | sort | head -1)"
    printf "   newest transcript %s\n" \
      "$(find ~/.claude/projects -name '*.jsonl' -exec stat -f '%Sm' -t '%Y-%m-%d' {} \; 2>/dev/null | sort | tail -1)"
  fi
  # Codex keeps its own per-thread token counter and never prunes rollouts, so
  # it is the highest-confidence recovery target on an old machine.
  for s in ~/.codex/state_*.sqlite; do
    [ -f "$s" ] && printf "   codex registry   %s (%s)\n" "$(basename $s)" "$(du -h $s | cut -f1)"
  done
  printf "   oldest rollout   %s\n" \
    "$(ls -d ~/.codex/sessions/*/*/*/ 2>/dev/null | head -1)"
  [ -f ~/.agentsview/sessions.db ] && printf "   agentsview       %s\n" "$(du -h ~/.agentsview/sessions.db | cut -f1)"
REMOTE

mkdir -p "$ARCHIVE"
pull() {  # pull <remote-path> <local-subdir>
  local remote="$1" name="$2"
  echo "==> $name"
  mkdir -p "$ARCHIVE/$name"
  rsync -az --update --partial --info=stats1 \
        --include='*/' --include='*.jsonl' --include='*.json' \
        --include='*.db' --include='*.sqlite' --exclude='*' \
        "${REMOTE_USER}@${MB1}:${remote}/" "$ARCHIVE/$name/" 2>&1 | tail -2
}

pull '~/.claude/projects'      "claude-main"
pull '~/.claude-accounts'      "claude-accounts"
pull '~/.session-vc'           "session-vc"
pull '~/.session-gt'           "session-gt"
pull '~/.codex/sessions'       "codex-sessions"
pull '~/.grok'                 "grok"
pull '~/.gemini'               "gemini"
pull '~/.qwen'                 "qwen"
pull '~/.ollama'               "ollama"
for p in $(ssh "${REMOTE_USER}@${MB1}" 'ls -d ~/.claude-* 2>/dev/null' | grep -v claude-accounts); do
  pull "$p" "claude-$(basename "$p" | sed 's/^\.claude-//')"
done

echo "==> aggregate archives (the only record of already-deleted eras)"
# These are DURABLE AGGREGATES, not transcripts — no retention sweep touches
# them. On a machine whose transcripts were pruned months ago they are usually
# the only surviving evidence, and they are tiny. Pull them even when the
# transcript trees come back empty.
mkdir -p "$ARCHIVE/_aggregates"
for f in '~/.claude/usage-checkpoint.json' '~/.claude/stats-cache.json' \
         '~/vc/.usage-cache.json' '~/vc/.usage-history.jsonl' \
         '~/.gt/costs.jsonl' '~/.agentsview/sessions.db' \
         '~/.codex/state_*.sqlite' '~/.codex/history.jsonl' \
         '~/.codex/session_index.jsonl'; do
  rsync -az --update "${REMOTE_USER}@${MB1}:$f" "$ARCHIVE/_aggregates/" 2>/dev/null || true
done

echo
echo "device archive: $ARCHIVE ($(du -sh "$ARCHIVE" 2>/dev/null | cut -f1), \
$(find "$ARCHIVE" -name '*.jsonl' | wc -l | tr -d ' ') transcripts)"
cat <<NEXT

next — the ORDER is load-bearing:
  cp ~/.flightdeck/usage.db ~/.flightdeck/usage.db.bak-\$(date +%Y%m%d)
  python3 -m flightdeck collect          # ingests devices/ trees, deduped by session+event id
  python3 -m flightdeck import-archive   # the AGGREGATES above; collect alone ignores them
  python3 -m flightdeck total            # the honest number (max-per-session on overlap)

collect WITHOUT import-archive understates the estate — the aggregates carry
eras whose transcripts no longer exist anywhere, and nothing else reads them.
NEXT
