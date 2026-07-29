"""Submit the leaderboard payload directly, without viberank-cli.

`npx viberank-cli` cannot be used to submit flightdeck's numbers. It runs
`npx ccusage@latest daily --json > cc.json` and posts *that* — so every
correction is discarded: months recovered from an index after Claude Code
deleted the transcripts, subagent burn the naive scanners double-count or
miss, and the archive reconciliation. It also guesses your identity from
`git config user.name`, which is a real name, not a GitHub handle.

So this posts the payload flightdeck built, to the same endpoint and with the
same headers the CLI uses (verified against viberank-cli 1.2.0 source):

    POST https://www.viberank.app/api/submit
    X-GitHub-User: <handle>      X-CLI-Version: <v>      X-Machine-Id: <uuid>

Two deliberate frictions, because this is public and irreversible:

* `--user` is REQUIRED and never inferred. Publishing tens of thousands of
  dollars of usage under the wrong handle is not a recoverable mistake.
* Nothing is sent without `--yes`. The default prints what *would* go and the
  handle it would go under, and exits.

The machine id is a random UUID persisted at `~/.viberank/machine-id`, the
same convention the CLI uses: the server sums across machines under one
account, and treats a repeat from the same machine as a replace rather than a
duplicate.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ENDPOINT = "https://www.viberank.app/api/submit"
CLI_VERSION = "1.2.0"
MACHINE_ID_FILE = Path.home() / ".viberank" / "machine-id"


def machine_id() -> str:
    """Stable anonymous per-machine id. Random UUID, no hardware info."""
    try:
        existing = MACHINE_ID_FILE.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    new = str(uuid.uuid4())
    try:
        MACHINE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        MACHINE_ID_FILE.write_text(new)
        os.chmod(MACHINE_ID_FILE, 0o600)
    except OSError:
        pass          # read-only home: ephemeral id, worst case one extra row
    return new


def post(payload: dict, user: str, endpoint: str = ENDPOINT) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-User": user,
            "X-CLI-Version": CLI_VERSION,
            "X-Machine-Id": machine_id(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:400]
        raise SystemExit(f"viberank returned {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"could not reach viberank: {e.reason}")


def run(path: str | Path, user: str | None, confirmed: bool = False) -> dict:
    src = Path(path).expanduser()
    if not src.exists():
        raise SystemExit(f"no payload at {src} — run `flightdeck export --viberank` first")
    payload = json.loads(src.read_text())
    totals = payload.get("totals", {})
    summary = {
        "days": len(payload.get("daily", [])),
        "tokens": totals.get("totalTokens", 0),
        "cost": totals.get("totalCost", 0.0),
        "user": user,
        "payload": str(src),
    }
    if not user:
        raise SystemExit(
            "--user is required and is never guessed. viberank-cli defaults to "
            "`git config user.name` (a real name, not a GitHub handle); "
            "submitting under the wrong handle cannot be undone."
        )
    if not confirmed:
        summary["sent"] = False
        return summary
    result = post(payload, user)
    summary["sent"] = True
    summary["response"] = result
    return summary
