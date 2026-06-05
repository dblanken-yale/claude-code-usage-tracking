---
description: Show your Claude Code token usage and estimated API cost (ROI report)
allowed-tools: Bash(python3:*)
---

Run the usage tracker's report and present the results to me.

Run the first command that succeeds:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/track-usage.py" --report
python3 ~/code/claude-code-usage-tracking/scripts/track-usage.py --report
```

If `$ARGUMENTS` contains a month (e.g. `2026-06`), pass `--month <YYYY-MM>`. If it
contains a dollar amount, pass it as `--sub-cost <N>` so the report includes the
subscription-vs-API breakeven verdict.

Then summarize the output for me: total actual cost, caching savings, the biggest
models and projects by cost, the 30-day run-rate, and (if a subscription cost was
given) whether subscription or API wins.
