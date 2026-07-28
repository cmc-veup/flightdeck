<p align="center">
  <img src="assets/hero.png" alt="Flightdeck: a LEGO control tower audits token bricks arriving from three provider aircraft" width="720">
</p>

# Flightdeck

A token-usage collector for local AI coding agents that reports numbers you can trust. It reads the raw transcripts that Claude Code, Codex CLI, and Grok CLI already write to disk, normalizes them into one SQLite database, and answers the only questions that matter: how many tokens, on which model, from which account, at what cost, and how fast is it burning.

## Why this exists

Every usage dashboard already on this machine was wrong in a different way, and most of the failures share a root cause: they could not see the fan-out architecture the machine actually runs.

- **Blind to subagents.** Tools that `rglob` every `*.jsonl` count a subagent transcript twice, once as its own session and once inside its parent, which makes the main-vs-subagent split unreportable. Flightdeck classifies every event (main / subagent / workflow subagent), attributes it to its parent session exactly once, and reports the architecture as a first-class dimension.
- **Stale caches.** One collector read a Claude stats cache that had not updated since February and reported it as current.
- **Unread Codex accounting.** Codex writes exact cumulative token counts (`token_count` events, including cached and reasoning tokens) into every rollout file. Prior tools counted characters instead, or read only the first 100 lines of each file.
- **Invisible layouts.** Workflow subagents live under `subagents/workflows/wf_*/`. Roughly 2,000 transcript files, a whole tier of the fan-out, were invisible to every earlier scanner.
- **Sync duplication.** Sessions synced across Macs appear under multiple paths. Flightdeck dedupes by `(sessionId, message uuid)`, not by file path.

Every one of those failure modes has a unit test.

## Subagents are the spend architecture

This estate runs as 5 to 30 parallel sessions, each fanning out subagents and workflow agents. That fan-out is not overhead on top of the "real" usage; it is how the work gets done, and at build time it accounted for roughly 30% of all tokens in a 24-hour window. A usage report that collapses it into a footnote misprices a third of the operation and hides the one dimension you would tune first. So `flightdeck report` treats query source as a primary axis: the headline carries the main-vs-subagent split for both tokens and cost, a spend-architecture table breaks it down per provider (with workflow subagents separated from plain ones), and a model-by-source table catches the common case where subagents run a different model than their parent. Parent attribution exists to make this measurable; preventing double-counts is the mechanism, not the point.

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
flightdeck export --viberank  # ccusage-shaped leaderboard payload (never auto-submits)
```

`collect` is incremental: a per-file mtime+size checkpoint in `~/.flightdeck/checkpoint.json` means a 5.7 GB corpus is scanned once, and each later run touches only files that changed. Re-parsing is idempotent; the unique event key makes duplicate inserts a no-op.

`report` leads with the main-vs-subagent split (tokens and cost, with each side's share), then breaks the window down three ways: a spend-architecture table (provider by source, with workflow subagents separated from plain ones), model by source, and account root. Cache read/write totals include the 5m/1h TTL breakdown; burn is reported as dollars per day and tokens per hour. `--json` emits the same data for scripts; the `sources`, `spend_architecture`, and `by_model_source` keys are additive and stable.

## Leaderboards

`flightdeck export --viberank` writes a `ccusage --json`-shaped payload for [viberank](https://www.viberank.app), the public Claude Code usage leaderboard: Claude-provider rows only, all account roots merged into daily totals with per-model breakdowns and computed cost. Because these are the dedup-corrected numbers, the entry is defensible where naive scanners inflate totals by double-counting subagent transcripts. Flightdeck never submits anything on its own: it writes the file and prints the two manual paths (GitHub sign-in upload at viberank.app, or `npx viberank-cli`). If you do submit, the only data that leaves the machine is aggregate daily token counts, model names, and computed USD cost. No prompts, no file paths, no project or session names.

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
