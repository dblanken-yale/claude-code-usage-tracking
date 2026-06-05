# Claude Code Usage Tracker

A Claude Code plugin that logs **per-turn token usage and estimated API cost** for
every session to a CSV, so you can analyze your spend and compare what the API would
cost against a subscription plan.

It runs entirely locally via hooks. No network calls, no dependencies (Python 3
standard library only).

## What it records

One row per assistant turn, appended to `~/.claude/usage/usage.csv`:

| Column | Meaning |
| --- | --- |
| `timestamp`, `date` | When the turn happened (ISO + `YYYY-MM-DD`) |
| `session_id` | The session the turn belongs to |
| `project`, `cwd` | Working directory (basename + full path) |
| `model` | The model that produced the turn (e.g. `claude-opus-4-8`) |
| `input_tokens`, `output_tokens` | Uncached input + generated output |
| `cache_creation_5m_tokens`, `cache_creation_1h_tokens` | Cache writes by TTL |
| `cache_read_tokens` | Cache reads |
| `total_tokens` | Sum of the above |
| `cost_usd` | Estimated API cost for this turn (cache tiers applied) |
| `cost_uncached_usd` | What it would have cost with caching disabled |
| `cc_version`, `message_id` | Claude Code version + API message id |

`cost_uncached_usd - cost_usd` is what prompt caching saved you.

Each turn is priced by **its own model**, so cheaper subagent/team turns (e.g. Haiku)
are costed correctly even when the main session is on Opus.

## Install

In Claude Code:

```
/plugin marketplace add dblanken-yale/claude-code-usage-tracking
/plugin install claude-code-usage-tracker@usage-tracker-marketplace
```

Enabling the plugin auto-registers its `Stop` and `SessionEnd` hooks — no manual
settings edits, no paths to configure. From then on, every session is logged to
`~/.claude/usage/usage.csv`. Nothing in the plugin is hardcoded to a particular
machine: data goes under your own home directory, and the script is found via
`${CLAUDE_PLUGIN_ROOT}` wherever Claude Code installs it.

To test locally before publishing, add the marketplace from the directory you cloned
into (any path works):

```
/plugin marketplace add /path/to/your/clone/claude-code-usage-tracking
/plugin install claude-code-usage-tracker@usage-tracker-marketplace
```

## Usage

### Report (easiest — no paths)

Run the bundled slash command in any session:

```
/usage-report
/usage-report 2026-06 200
```

It prints totals, caching savings, per-model and per-project breakdowns, average
cost per session/turn/day, a 30-day run-rate, and — when you pass a subscription
price — a subscription-vs-API breakeven verdict.

### Analyze in a spreadsheet

Open `~/.claude/usage/usage.csv` in Excel / Sheets and pivot on `date`, `model`,
`project`, or `session_id`. Sum `cost_usd` for your API-equivalent spend.

### Command line

The script records its own location on every run, so you never need to know where
the plugin lives:

```
python3 "$(cat ~/.claude/usage/.script_path)" --report
python3 "$(cat ~/.claude/usage/.script_path)" --report --month 2026-06
python3 "$(cat ~/.claude/usage/.script_path)" --report --sub-cost 200
python3 "$(cat ~/.claude/usage/.script_path)" --scan-all
```

`--scan-all` ingests every existing transcript on disk (safe to re-run — it never
double-counts), backfilling history and sweeping up subagent/team transcripts that
live in separate files. (`.script_path` is written the first time any hook fires; if
you want to back-fill before your first logged session, run the script directly from
your clone instead.)

Or use the bundled slash command in any session: `/usage-report` (optionally
`/usage-report 2026-06 200`).

## How it works

`Stop` and `SessionEnd` hooks run `scripts/track-usage.py`, which locates the
session transcript (`~/.claude/projects/<slug>/<session-id>.jsonl`) and ingests new
turns using a **per-transcript byte-offset cursor** stored in
`~/.claude/usage/.state.json`. It only ever reads new bytes, so writing after every
turn stays cheap and never double-counts. Concurrent sessions are serialized with a
file lock. The script always exits 0 and fails quietly, so it can't block a turn.

## Pricing

Rates are defined at the top of `scripts/track-usage.py` (USD per 1M tokens):

| Model family | Input | Output |
| --- | --- | --- |
| Opus | $5.00 | $25.00 |
| Sonnet | $3.00 | $15.00 |
| Haiku | $1.00 | $5.00 |

Cache reads are billed at 0.1x input; cache writes at 1.25x (5-minute) or 2x
(1-hour). Update the `PRICING` table if Anthropic's prices change. Unknown models
are still logged (token counts intact) with a blank cost so nothing is mispriced.

## Privacy

The CSV records **only** project paths, model ids, token counts, and computed cost.
It never stores prompt or response content. All data stays on your machine under
`~/.claude/usage/`.

## License

MIT
