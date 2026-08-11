<p align="center">
  <img src="assets/hero.png" alt="Flightdeck: a LEGO control tower audits token bricks arriving from a fleet of provider aircraft" width="720">
</p>

# Flightdeck

A token-usage collector for local AI coding agents that reports numbers you can trust. It reads the transcripts and usage stores that local agents already write to disk — Claude Code and every provider driven through its shell (DeepSeek, Ollama, Kimi, GLM, MiniMax, Qwen), plus Codex CLI, Grok CLI and Gemini — normalizes them into one SQLite database, and answers the only questions that matter: how many tokens, on which model, from which account, at what cost, and how fast is it burning.

## Why this exists

Every usage dashboard already on this machine was wrong in a different way, and most of the failures share a root cause: they could not see the fan-out architecture the machine actually runs.

- **Blind to the fan-out.** Earlier tools could not tell you what share of spend runs through subagents, which is the first number you would want. Flightdeck classifies every event as main, subagent, or workflow subagent and reports that split as a primary axis. (Attributing each event to its parent exactly once is how the classification stays honest — a transcript counted twice would corrupt both the total and the split. That is plumbing, not the feature.)
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
git clone https://github.com/cmc-veup/flightdeck
cd flightdeck
pip install -e .        # or run in place: python3 -m flightdeck ...
```

## Usage

```bash
flightdeck collect            # incremental scan into ~/.flightdeck/usage.db
flightdeck collect --full     # ignore the checkpoint, re-scan everything
flightdeck report             # human table, last 24h
flightdeck report --since 7d --json   # --since takes any Nh or Nd window (48h, 90d, ...)
flightdeck doctor             # which sources exist, freshness, row counts
flightdeck export --viberank  # ccusage-shaped leaderboard payload (never auto-submits)
```

`collect` is incremental: a per-file mtime+size checkpoint in `~/.flightdeck/checkpoint.json` means a 5.7 GB corpus is scanned once, and each later run touches only files that changed. Re-parsing is idempotent; the unique event key makes duplicate inserts a no-op.

`report` leads with the main-vs-subagent split (tokens and cost, with each side's share). The split's shares are computed within the providers that record a delegation dimension — codex and grok write `is_sidechain=0` unconditionally, so their tokens are reported alongside the split (and in every total) but never inside its denominator, where a codex-heavy day would dilute the subagent ratio into a phantom capture failure. It then breaks the window down three ways: a spend-architecture table (provider by source, with workflow subagents separated from plain ones), model by source, and account root. Cache read/write totals include the 5m/1h TTL breakdown; burn is reported as dollars per day and tokens per hour. `--json` emits the same data for scripts; the `sources`, `spend_architecture`, and `by_model_source` keys are additive and stable.

## Leaderboards

`flightdeck export --viberank` writes a `ccusage --json`-shaped payload for [viberank](https://www.viberank.app), the public coding-agent usage leaderboard: every provider (viberank has an all-models view), all account roots merged into daily totals with per-model breakdowns and computed cost. Subagent tokens are included — they are billed API calls and roughly a third of this estate — and sessions recovered from the archive are folded in, so the payload reconciles to `flightdeck total` rather than to whatever happens to survive on disk. To publish it:

```bash
flightdeck submit --viberank --user <your-github-handle>          # prints what would go, sends nothing
flightdeck submit --viberank --user <your-github-handle> --yes    # actually publishes
```

Use this rather than `npx viberank-cli`. The CLI regenerates the payload with `npx ccusage@latest daily --json` and posts *that*, discarding every correction — recovered months, subagent burn, archive reconciliation — and it guesses your identity from `git config user.name`, which is a real name, not a GitHub handle. `flightdeck submit` posts the file you just built, to the same endpoint and headers (verified against viberank-cli 1.2.0), and `--user` is required rather than inferred: publishing tens of thousands of dollars of usage under the wrong handle is not recoverable. Nothing is sent without `--yes`. If you do submit, the only data that leaves the machine is aggregate daily token counts, model names, and computed USD cost. No prompts, no file paths, no project or session names.

## Multiple devices

**One command per device:**

```bash
git clone https://github.com/cmc-veup/flightdeck && cd flightdeck
scripts/setup-device.sh --sync-repo ~/usage-repo     # omit --sync-repo to stay local
```

It sets `cleanupPeriodDays` so Claude Code stops deleting your history (backing up `settings.json` first), runs the first collect, installs an hourly job (launchd on macOS, cron elsewhere) that collects and — with `--sync-repo` — exports and pushes this device's rows, then prints what to run next. Idempotent: re-running is safe.

Then on any machine: `git pull && for f in devices/*.jsonl; do flightdeck merge "$f"; done && flightdeck total`.

**For a team**, it is the same shape with one private repo and one file per person (`devices/<name>.jsonl`). Each engineer runs the line above pointed at the shared repo; nobody's file collides with anyone else's, and any member can produce the combined total or their own leaderboard payload.


Each machine reads only its own transcripts, so each has a partial picture. Three ways to get one number, in ascending order of how much you have to trust the network:

```bash
# 1. git as the transport — no rsync, no ssh, no daemon (recommended)
flightdeck collect
flightdeck export --rows --out repo/devices/$(hostname -s).jsonl
cd repo && git add -A && git commit -m sync && git push
#    ...then anywhere you want the combined number:
git pull && for f in devices/*.jsonl; do flightdeck merge "$f"; done && flightdeck total

# 2. merge a database directly, if the machines can see each other
flightdeck merge /path/to/other-machine-usage.db --device laptop

# 3. pull raw transcripts off another host, then collect locally
scripts/grab-device.sh mb1.local mb1     # rsync over ssh into the archive
```

Option 1 needs no network code in flightdeck at all: each device writes **only its own file**, so two machines pushing at once touch different paths and git never has to merge anything. NDJSON rather than the SQLite file, because it diffs, compresses (~11:1 — a 15,500-event day is 6 MB raw, 675 KB gzipped), and survives a three-way merge if one ever does happen.

`cwd` is **redacted by default** in `--rows`: working directories carry client and project names, and this file is built to leave the machine. `--include-cwd` opts back in for a private repo you control.

Dedupe is structural, not heuristic: rows are keyed `(provider, session_id, event_id)`, so a session that exists on two devices — the common case, since transcripts sync — merges to one copy, and re-running a merge is a no-op. `--device` labels incoming rows `<device>:<root>` if you want per-machine reporting.

Do **not** share one `usage.db` over Dropbox or Syncthing and write to it from several machines. SQLite over a file-sync layer corrupts.

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

Pricing lives in a `pricing` table with per-Mtoken input, output, cache-read, and cache-write USD. Rows seeded from provider list prices carry `is_estimate = 0`; anything inferred (legacy tiers, grok, deepseek, and the smaller Chinese-lab models) is marked `is_estimate = 1` and flagged with `*` in reports. Grok costs are provider-reported and bypass the table entirely.

Rates are **effective-dated**: rows carry `valid_from` / `valid_to`, and a token is priced at the rate in force on the day it was spent, not today's rate. Anthropic list prices were verified against 17 archived snapshots of the pricing page spanning 2026-02-01 to 2026-07-28 — no Claude per-token price moved in that window, so the Opus family prices flat throughout. Sonnet 5 is the live case: its introductory $2/$10 runs through 2026-08-31, and the $3/$15 sticker applies from 2026-09-01.

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
