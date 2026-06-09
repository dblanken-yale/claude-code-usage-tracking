---
description: Show your Claude Code token usage and estimated API cost (ROI report)
allowed-tools: Bash(python3:*), Bash(cat:*)
---

Run the usage tracker's report and present the results to me.

Locate the tracker script without assuming an install path — run the first command
that succeeds:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/track-usage.py" --report
python3 "$(cat ~/.claude/usage/.script_path)" --report
```

(The second works because the script records its own location on every run.)

If `$ARGUMENTS` contains a month (e.g. `2026-06`), append `--month <YYYY-MM>`. If it
contains a dollar amount, append `--sub-cost <N>` so the report includes the
subscription-vs-API breakeven verdict.

Then summarize the output for me: total actual cost, caching savings, the biggest
models and projects by cost, the 30-day run-rate, and (if a subscription cost was
given) whether subscription or API wins.

## Pricing

Costs are computed per model VERSION (e.g. Opus 4.1 bills at the old $15/$75, Opus 4.5+
at $5/$25), using a version-aware baseline table in the script plus a cached overlay
fetched from Anthropic's public pricing page (`~/.claude/usage/prices.json`). The report
prints which source it used on the `Prices:` line, and refreshes the cache opportunistically.
New model prices are also fetched automatically the first time an unseen model is logged.

- `--refresh-prices` — force a refresh of the cached prices now.
- `--reprice` — recompute the cost columns in `usage.csv` from recorded tokens using current
  pricing (backs up to `usage.csv.bak` first). Run this after prices change to correct history.
