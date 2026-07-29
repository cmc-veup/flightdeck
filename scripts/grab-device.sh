#!/usr/bin/env bash
# Pull EVERY agent transcript and usage store off mb1 into the archive.
#
#   ./scripts/grab-mb1.sh <mb1-hostname-or-ip> [remote-user]
#   ./scripts/grab-mb1.sh mb1.local
#
# Prereq on mb1: System Settings → General → Sharing → Remote Login (SSH) on.
#
# Safety: everything lands under ~/transcript-archive/mb1/ — a SEPARATE tree.
# Nothing on this machine is overwritten, and rsync runs without --delete, so
# this can be re-run any number of times and can only ever add data. mb1's own
# Claude Code may have pruned its recent transcripts, but anything it still
# holds for April/May 2026 exists nowhere else.
set -uo pipefail

MB1="${1:-}"
REMOTE_USER="${2:-mchack}"
ARCHIVE="${TRANSCRIPT_ARCHIVE:-$HOME/transcript-archive}/mb1"

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
mkdir -p "$ARCHIVE/_aggregates"
for f in '~/.claude/usage-checkpoint.json' '~/vc/.usage-cache.json' \
         '~/vc/.usage-history.jsonl' '~/.gt/costs.jsonl'; do
  rsync -az --update "${REMOTE_USER}@${MB1}:$f" "$ARCHIVE/_aggregates/" 2>/dev/null || true
done

echo
echo "mb1 archive: $ARCHIVE ($(du -sh "$ARCHIVE" 2>/dev/null | cut -f1), \
$(find "$ARCHIVE" -name '*.jsonl' | wc -l | tr -d ' ') transcripts)"
echo "next: flightdeck collect   # ingests the mb1 tree, deduping by session+event id"
