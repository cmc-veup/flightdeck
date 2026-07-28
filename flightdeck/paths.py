"""Data-source roots and flightdeck home paths."""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("FLIGHTDECK_HOME_OVERRIDE", str(Path.home())))

FLIGHTDECK_DIR = Path(os.environ.get("FLIGHTDECK_DIR", str(HOME / ".flightdeck")))
DB_PATH = FLIGHTDECK_DIR / "usage.db"
CHECKPOINT_PATH = FLIGHTDECK_DIR / "checkpoint.json"

# (root, provider, account_root_label) — Claude-Code-format JSONL trees.
# Any provider driven through a Claude Code shell (CLAUDE_CONFIG_DIR +
# ANTHROPIC_BASE_URL) inherits full Claude-format fidelity; attribution is by
# config-dir root.
CLAUDE_ROOTS: list[tuple[Path, str, str]] = [
    (HOME / ".claude" / "projects", "claude", "main"),
    (HOME / ".claude-accounts" / "veup" / "projects", "claude", "veup"),
    (HOME / ".claude-accounts" / "pmme" / "projects", "claude", "pmme"),
    (HOME / ".claude-deepseek" / "projects", "deepseek", "deepseek"),
    (HOME / ".claude-ollama" / "projects", "ollama", "ollama"),
]

CODEX_SESSIONS = HOME / ".codex" / "sessions"
GROK_DB = HOME / ".grok" / "grok.db"


def ensure_dirs() -> None:
    FLIGHTDECK_DIR.mkdir(parents=True, exist_ok=True)
