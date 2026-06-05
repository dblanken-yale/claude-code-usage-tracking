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
