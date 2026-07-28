<p align="center">
  <img src="assets/hero.png" alt="Flightdeck: a LEGO control tower audits token bricks arriving from three provider aircraft" width="720">
</p>

# Flightdeck

A token-usage collector for local AI coding agents that reports numbers you can trust. It reads the raw transcripts that Claude Code, Codex CLI, and Grok CLI already write to disk, normalizes them into one SQLite database, and answers the only questions that matter: how many tokens, on which model, from which account, at what cost, and how fast is it burning.

## Why this exists

Every usage dashboard already on this machine was wrong in a different way:

- **Subagent double-counting.** Tools that `rglob` every `*.jsonl` count a subagent transcript twice: once as its own session and once inside its parent. Flightdeck attributes each subagent event to its parent session exactly once, with a sidechain flag so the share is still visible.
- **Stale caches.** One collector read a Claude stats cache that had not updated since February and reported it as current.
- **Unread Codex accounting.** Codex writes exact cumulative token counts (`token_count` events, including cached and reasoning tokens) into every rollout file. Prior tools counted characters instead, or read only the first 100 lines of each file.
- **Invisible layouts.** Workflow subagents live under `subagents/workflows/wf_*/`. Roughly 2,000 transcript files were invisible to every earlier scanner.
- **Sync duplication.** Sessions synced across Macs appear under multiple paths. Flightdeck dedupes by `(sessionId, message uuid)`, not by file path.

Every one of those failure modes has a unit test.

## Install

Python 3.11+, standard library plus SQLite. No third-party dependencies.

```bash
git clone https://github.com/justakeyboardbetweenus/flightdeck
cd flightdeck
pip install -e .        # or run in place: python3 -m flightdeck ...
```

## Usage

```bash
flightdeck collect            # incremental scan into ~/.flightdeck/usage.db
flightdeck collect --full     # ignore the checkpoint, re-scan everything
flightdeck report             # human table, last 24h
flightdeck report --since 7d --json
flightdeck doctor             # which sources exist, freshness, row counts
```

`collect` is incremental: a per-file mtime+size checkpoint in `~/.flightdeck/checkpoint.json` means a 5.7 GB corpus is scanned once, and each later run touches only files that changed. Re-parsing is idempotent; the unique event key makes duplicate inserts a no-op.

`report` shows totals by provider, model, and account root, the cache read/write split (including the 5m/1h TTL breakdown), the subagent share, dollars per day, and tokens per hour for the window. `--json` emits the same data for scripts.

## Data sources

| Source | Path | What is read |
|---|---|---|
| Claude Code (main) | `~/.claude/projects/**/*.jsonl` | Per-message usage: input, output, cache write (5m/1h split), cache read, model, timestamp, sidechain attribution |
| Claude Code (veup, pmme) | `~/.claude-accounts/{veup,pmme}/projects/**` | Same format, attributed per account root |
| DeepSeek | `~/.claude-deepseek/projects/**` | Same format (Claude Code shell), provider `deepseek` |
| Ollama | `~/.claude-ollama/projects/**` | Same format, provider `ollama` (scanned when present) |
| Codex CLI | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | Last `token_count` per rollout (input, cached input, output, reasoning), model from `turn_context`, cwd, plus the latest rate-limit/plan snapshot |
| Grok CLI | `~/.grok/grok.db` | `usage_events` rows: tokens and provider-reported `cost_micros` |

Any provider driven through a Claude Code shell (`CLAUDE_CONFIG_DIR` + `ANTHROPIC_BASE_URL`) inherits full Claude-format fidelity; adding one is a single line in `flightdeck/paths.py`.

## Storage

Normalized rows land in `~/.flightdeck/usage.db` (schema in `flightdeck/db.py`). Flightdeck deliberately does not write into agentsview's `sessions.db`: that table has a foreign key into agentsview's own session index and cannot express provider, account root, sidechain attribution, or the cache TTL split.

Pricing lives in a `pricing` table with per-Mtoken input, output, cache-read, and cache-write USD. Rows seeded from provider list prices carry `is_estimate = 0`; anything inferred (legacy tiers, gpt-5.x, grok, deepseek) is marked `is_estimate = 1` and flagged with `*` in reports. Grok costs are provider-reported and bypass the table entirely.

## Verifying the numbers

`scripts/verify_recount.py` is an independent recount: it re-sums the raw JSONL for a time window without importing the collector, deduping the same way. Run both against a pinned window end and the totals must match exactly:

```bash
NOW=$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]+'Z')")
python3 scripts/verify_recount.py --hours 24 --now "$NOW"
flightdeck report --since 24h --json --now "$NOW"
```

Unit tests cover the parser edge cases (sidechain attribution, workflow layouts, cross-Mac dedupe, journal and synthetic-model skipping):

```bash
python3 -m unittest discover -s tests
```

## Roadmap

Phase 0 (this repo) makes the historical numbers true. Later phases, tracked in the flightdeck plan:

- surface these numbers through `caut` so the existing wave-spawn checks tell the truth
- a live OTEL layer (Claude Code native telemetry, per-subagent labels, account attribution)
- Gemini CLI coverage via OTLP, the one remaining blind spot
- the Emerging Architecture Index: adoption and emergence metrics on top of the corrected data

## License

MIT with AI-Lab Rider: free for humans and companies; all rights void for OpenAI/Anthropic and affiliates, including any dataset, training, or evaluation use. See [LICENSE](LICENSE).
