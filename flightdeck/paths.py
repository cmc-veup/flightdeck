"""Data-source roots and flightdeck home paths."""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("FLIGHTDECK_HOME_OVERRIDE", str(Path.home())))

FLIGHTDECK_DIR = Path(os.environ.get("FLIGHTDECK_DIR", str(HOME / ".flightdeck")))
DB_PATH = FLIGHTDECK_DIR / "usage.db"
CHECKPOINT_PATH = FLIGHTDECK_DIR / "checkpoint.json"

# Backends that are a DIFFERENT model provider wearing the Claude Code shell
# (CLAUDE_CONFIG_DIR + ANTHROPIC_BASE_URL). Anything else — mcc22, nexym,
# burst, sync — is another Claude account, so it stays provider "claude".
NON_CLAUDE_BACKENDS = {
    "deepseek", "ollama", "kimi", "kimik2", "qwen", "minimax",
    "zai", "glm", "mistral", "groq", "together", "openrouter",
}


def discover_claude_roots(home: Path | None = None) -> list[tuple[Path, str, str]]:
    """Find every Claude-Code-format transcript tree on this machine.

    Discovery, not a hardcoded list: every account profile and every
    provider-behind-a-Claude-shell writes to its own `CLAUDE_CONFIG_DIR`,
    and new ones appear whenever a pool or profile is added (gas-city pools,
    caam profiles, a future Kimi backend). A static list silently under-counts
    the estate the day after it's written — which is the whole failure mode
    flightdeck exists to fix.

    Returns (projects_dir, provider, account_root_label), deduped, sorted.
    """
    h = home or HOME
    found: dict[Path, tuple[str, str]] = {}

    main = h / ".claude" / "projects"
    found[main] = ("claude", "main")

    # ~/.claude-accounts/<name>/projects — caam-managed account profiles.
    for d in sorted((h / ".claude-accounts").glob("*")):
        if (d / "projects").is_dir():
            found[d / "projects"] = ("claude", d.name)

    # ~/.claude-<name>/projects — per-provider / per-profile config dirs.
    for d in sorted(h.glob(".claude-*")):
        if d.name == ".claude-accounts" or not (d / "projects").is_dir():
            continue
        label = d.name[len(".claude-"):]
        # Prefix match so `.claude-deepseek-test` / `.claude-kimi-2` attribute
        # to their real backend instead of silently reading as a Claude account.
        base = label.split("-", 1)[0]
        provider = base if base in NON_CLAUDE_BACKENDS else "claude"
        found[d / "projects"] = (provider, label)

    # Session archives that live outside any CLAUDE_CONFIG_DIR: Syncthing-
    # replicated transcript folders carrying sessions from the other machine.
    # Same Claude JSONL format; the (provider, session_id, event_id) primary
    # key makes re-ingesting an already-counted session a no-op, so scanning
    # them can only ADD what was missing.
    for extra, label in ((h / ".session-vc", "session-vc"),
                         (h / ".session-gt", "session-gt")):
        if extra.is_dir():
            found[extra] = ("claude", label)

    # Transcript trees pulled off OTHER DEVICES (scripts/grab-device.sh writes
    # <archive>/<device>/<tree>). Ingesting them is safe by construction:
    # identical events collide on the (provider, session_id, event_id) primary
    # key and are ignored, so a session synced to two machines counts once.
    # ONLY under <archive>/devices/ — the archive root also holds this machine's
    # own append-only mirror, and scanning that re-ingests local transcripts
    # under a different provider label, which defeats the primary key and
    # double-counts them. Other devices go in devices/<name>/ (grab-device.sh).
    archive = Path(os.environ.get("TRANSCRIPT_ARCHIVE", str(h / "transcript-archive")))
    for device in sorted((archive / "devices").glob("*")):
        if not device.is_dir() or device.name.startswith("_"):
            continue
        for tree in sorted(device.glob("*")):
            if tree.is_dir() and not tree.name.startswith("_") and any(tree.rglob("*.jsonl")):
                found[tree] = ("claude", f"{device.name}:{tree.name}")

    return [(p, prov, label) for p, (prov, label) in sorted(found.items())]


# (root, provider, account_root_label) — Claude-Code-format JSONL trees.
CLAUDE_ROOTS: list[tuple[Path, str, str]] = discover_claude_roots()

CODEX_SESSIONS = HOME / ".codex" / "sessions"


def discover_codex_roots(home: Path | None = None) -> list[tuple[Path, str]]:
    """Every Codex session tree on this machine, as (sessions_dir, account_label).

    Discovery, not a hardcoded path — for the same reason discover_claude_roots
    exists. A second Codex account arrives one of two ways and only one of them
    is visible to a fixed `~/.codex`:

      * `caam add codex <name>` swaps auth in place, so both accounts share
        ~/.codex/sessions. Captured either way, but indistinguishable — the same
        attribution loss Claude has under `caam activate`.
      * `caam exec codex <name>` (and any CODEX_HOME override) gives the profile
        its own HOME, so its rollouts land somewhere else entirely and a fixed
        path misses 100% of them.

    That second case is exactly how ~/.kimi-code went uncollected: the collector
    knew one location and the tool wrote to another. Returns labels so each
    account gets its own account_root instead of collapsing into one bucket.
    """
    h = home or HOME
    found: dict[Path, str] = {}

    env = os.environ.get("CODEX_HOME")
    if env:
        p = Path(env).expanduser() / "sessions"
        if p.is_dir():
            found[p] = "codex"

    default = h / ".codex" / "sessions"
    if default.is_dir():
        found[default] = "codex"

    # ~/.codex-<name>/sessions — mirrors the ~/.claude-<name> convention.
    for d in sorted(h.glob(".codex-*")):
        if (d / "sessions").is_dir():
            found[d / "sessions"] = d.name[len(".codex-"):]

    # caam's isolated ("shallow") profile homes: ~/.caam-shallow/<profile>/.codex
    for d in sorted((h / ".caam-shallow").glob("*")):
        p = d / ".codex" / "sessions"
        if p.is_dir():
            found[p] = f"caam:{d.name}"

    return [(p, label) for p, label in sorted(found.items())]


CODEX_ROOTS: list[tuple[Path, str]] = discover_codex_roots()
# Kimi Code CLI: its own tree, NOT a Claude-shell backend. The kimi-k2.5 rows
# already in the DB came through CLAUDE_CONFIG_DIR pools and are labelled
# provider "claude"; this is the standalone CLI and lands as provider "kimi".
KIMI_SESSIONS = HOME / ".kimi-code" / "sessions"
GROK_DB = HOME / ".grok" / "grok.db"


def ensure_dirs() -> None:
    FLIGHTDECK_DIR.mkdir(parents=True, exist_ok=True)
