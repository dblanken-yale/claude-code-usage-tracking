#!/usr/bin/env python3
"""Claude Code token & cost usage tracker.

Logs one row per assistant turn for every Claude Code session to
~/.claude/usage/usage.csv, with an estimated API-equivalent cost per row, so you
can analyze spend in Excel and compare API cost against a subscription plan.

Modes:
  (default)   Hook mode. Reads hook JSON from stdin (Stop / SessionEnd),
              locates the session transcript, and ingests any new turns.
  --scan-all  Walk every ~/.claude/projects/*/*.jsonl and ingest. Backfills
              history and sweeps up subagent/team transcript files.
  --report    Print an ROI rollup from usage.csv (no transcript parsing).
              Options: --month YYYY-MM, --sub-cost N

Two-layer design:
  * Performance: a per-transcript byte offset in ~/.claude/usage/.state.json means
    each run only reads new bytes, so logging after every turn stays cheap.
  * Correctness: a persisted set of seen message ids in ~/.claude/usage/.seen_ids
    means each billed API message (message.id) is counted exactly once.

The seen-id set is needed because (a) Claude Code writes one JSONL line per content
block (thinking / text / tool_use) and every line of a message repeats the same
message.id and usage totals, and (b) resuming or forking a session replays prior
messages — with their original ids and usage — into a new transcript file. Both would
otherwise multiply a turn's tokens. Deduping globally on message.id handles all of it,
including the offset boundary between hook runs.

It always exits 0 and fails quietly, so it can never block a turn.
"""

import argparse
import csv
import fcntl
import glob
import json
import os
import sys

# --- Pricing: USD per 1,000,000 tokens. UPDATE WHEN ANTHROPIC PRICES CHANGE. --
# Matched by substring against the model id (4.x Opus/Sonnet families share
# rates). First match wins, so list more specific keys first if rates diverge.
PRICING = [
    ("opus", 5.00, 25.00),
    ("sonnet", 3.00, 15.00),
    ("haiku", 1.00, 5.00),
]
CACHE_READ_MULT = 0.10  # cache-read tokens billed at 0.1x the input rate
CACHE_5M_MULT = 1.25    # 5-minute ephemeral cache write
CACHE_1H_MULT = 2.00    # 1-hour ephemeral cache write

USAGE_DIR = os.path.expanduser("~/.claude/usage")
CSV_PATH = os.path.join(USAGE_DIR, "usage.csv")
STATE_PATH = os.path.join(USAGE_DIR, ".state.json")
SEEN_PATH = os.path.join(USAGE_DIR, ".seen_ids")
LOCK_PATH = os.path.join(USAGE_DIR, ".lock")
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")

FIELDS = [
    "timestamp", "date", "session_id", "project", "cwd", "model",
    "input_tokens", "output_tokens", "cache_creation_5m_tokens",
    "cache_creation_1h_tokens", "cache_read_tokens", "total_tokens",
    "cost_usd", "cost_uncached_usd", "cc_version", "message_id",
]


def rates_for(model):
    for key, in_rate, out_rate in PRICING:
        if key in model:
            return in_rate, out_rate
    return None, None


def compute_costs(model, inp, out, read, cc5m, cc1h):
    """Return (cost_usd, cost_uncached_usd). Empty strings if model unknown."""
    in_rate, out_rate = rates_for(model)
    if in_rate is None:
        return "", ""
    cost = (
        inp * in_rate
        + out * out_rate
        + read * in_rate * CACHE_READ_MULT
        + cc5m * in_rate * CACHE_5M_MULT
        + cc1h * in_rate * CACHE_1H_MULT
    ) / 1_000_000
    gross = (
        (inp + read + cc5m + cc1h) * in_rate + out * out_rate
    ) / 1_000_000
    return round(cost, 6), round(gross, 6)


def row_from_record(d):
    """Build a CSV row dict from a transcript line, or None if not billable."""
    if d.get("type") != "assistant":
        return None
    msg = d.get("message") or {}
    usage = msg.get("usage")
    model = msg.get("model")
    if not usage or not model or model == "<synthetic>":
        return None

    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    read = usage.get("cache_read_input_tokens") or 0
    cc = usage.get("cache_creation") or {}
    cc5m = cc.get("ephemeral_5m_input_tokens") or 0
    cc1h = cc.get("ephemeral_1h_input_tokens") or 0
    # Fall back to the flat field if the 5m/1h breakdown is absent (count as 5m).
    if not cc and usage.get("cache_creation_input_tokens"):
        cc5m = usage.get("cache_creation_input_tokens") or 0

    ts = d.get("timestamp", "") or ""
    cwd = d.get("cwd", "") or ""
    cost, gross = compute_costs(model, inp, out, read, cc5m, cc1h)
    return {
        "timestamp": ts,
        "date": ts[:10],
        "session_id": d.get("sessionId", ""),
        "project": os.path.basename(cwd.rstrip("/")) if cwd else "",
        "cwd": cwd,
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_5m_tokens": cc5m,
        "cache_creation_1h_tokens": cc1h,
        "cache_read_tokens": read,
        "total_tokens": inp + out + read + cc5m + cc1h,
        "cost_usd": cost,
        "cost_uncached_usd": gross,
        "cc_version": d.get("version", ""),
        "message_id": msg.get("id", ""),
    }


def load_state():
    """Per-transcript byte offsets: {path: offset}. Tolerates the legacy
    {path: {offset, last_id}} shape from earlier versions."""
    try:
        with open(STATE_PATH) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    state = {}
    for path, val in raw.items():
        state[path] = val.get("offset", 0) if isinstance(val, dict) else val
    return state


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def load_seen():
    try:
        with open(SEEN_PATH) as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def process(path, state, writer, seen, seen_fh):
    """Ingest new (offset-onward) complete lines from one transcript file,
    skipping any message id already counted anywhere (seen set)."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return 0
    offset = state.get(path, 0)
    size = os.path.getsize(path)
    if offset > size:  # file shrank / rotated — re-read from the start
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    last_nl = data.rfind(b"\n")
    if last_nl == -1:  # no complete line yet
        return 0
    consumed = data[: last_nl + 1]
    rows = 0
    for raw in consumed.split(b"\n"):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        row = row_from_record(rec)
        if not row:
            continue
        mid = row["message_id"]
        if mid:
            if mid in seen:  # already counted (continuation line, resume, or fork)
                continue
            seen.add(mid)
            seen_fh.write(mid + "\n")
        writer.writerow(row)
        rows += 1
    state[path] = offset + len(consumed)
    return rows


def ingest(paths):
    os.makedirs(USAGE_DIR, exist_ok=True)
    lock_fh = open(LOCK_PATH, "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    try:
        state = load_state()
        seen = load_seen()
        new_file = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, "a", newline="") as cf, open(SEEN_PATH, "a") as sf:
            writer = csv.DictWriter(cf, fieldnames=FIELDS)
            if new_file:
                writer.writeheader()
            total = sum(process(p, state, writer, seen, sf) for p in paths)
        save_state(state)
        return total
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def resolve_transcript(hook):
    """Prefer transcript_path; else reconstruct from session_id + cwd."""
    tp = hook.get("transcript_path")
    if tp:
        return os.path.expanduser(tp)
    sid = hook.get("session_id")
    cwd = hook.get("cwd")
    if sid and cwd:
        slug = cwd.replace("/", "-")
        return os.path.expanduser(f"~/.claude/projects/{slug}/{sid}.jsonl")
    return None


def hook_mode():
    try:
        hook = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    path = resolve_transcript(hook)
    if path:
        ingest([path])


def _f(row, key):
    try:
        return float(row[key]) if row[key] else 0.0
    except (ValueError, KeyError):
        return 0.0


def report(month=None, sub_cost=None):
    if not os.path.exists(CSV_PATH):
        print(f"No usage data yet at {CSV_PATH}")
        return
    rows = []
    with open(CSV_PATH, newline="") as f:
        for r in csv.DictReader(f):
            if month and not (r.get("date") or "").startswith(month):
                continue
            rows.append(r)
    if not rows:
        print("No rows for the selected period.")
        return

    total_cost = sum(_f(r, "cost_usd") for r in rows)
    total_gross = sum(_f(r, "cost_uncached_usd") for r in rows)
    savings = total_gross - total_cost
    sessions = {r["session_id"] for r in rows if r.get("session_id")}
    turns = len(rows)
    dates = sorted({r["date"] for r in rows if r.get("date")})
    days = max(1, len(dates))
    daily = total_cost / days
    run_rate = daily * 30

    by_model = {}
    by_project = {}
    for r in rows:
        by_model[r["model"]] = by_model.get(r["model"], 0.0) + _f(r, "cost_usd")
        by_project[r["project"]] = by_project.get(r["project"], 0.0) + _f(r, "cost_usd")

    scope = f"month {month}" if month else "all time"
    span = f"{dates[0]} -> {dates[-1]}" if dates else "n/a"
    pct = (savings / total_gross * 100) if total_gross else 0.0

    print(f"Claude Code usage report ({scope})")
    print(f"  Date span:           {span}  ({days} active day(s))")
    print(f"  Sessions:            {len(sessions)}")
    print(f"  Turns logged:        {turns}")
    print()
    print(f"  Actual API cost:     ${total_cost:,.2f}")
    print(f"  Gross (no caching):  ${total_gross:,.2f}")
    print(f"  Caching savings:     ${savings:,.2f}  ({pct:.1f}%)")
    print()
    print(f"  Avg cost / session:  ${(total_cost / len(sessions)) if sessions else 0:,.4f}")
    print(f"  Avg cost / turn:     ${(total_cost / turns) if turns else 0:,.4f}")
    print(f"  Avg cost / day:      ${daily:,.2f}")
    print(f"  30-day run-rate:     ${run_rate:,.2f}")
    print()
    print("  Cost by model:")
    for model, cost in sorted(by_model.items(), key=lambda kv: -kv[1]):
        print(f"    {model:<22} ${cost:,.2f}")
    print()
    print("  Top projects by cost:")
    top = sorted(by_project.items(), key=lambda kv: -kv[1])[:10]
    for project, cost in top:
        print(f"    {(project or '(unknown)'):<22} ${cost:,.2f}")

    if sub_cost is not None:
        print()
        delta = run_rate - sub_cost
        if delta > 0:
            print(f"  Subscription verdict: at this run-rate, API would cost "
                  f"${delta:,.2f}/mo MORE than your ${sub_cost:,.2f} plan "
                  f"-> subscription wins.")
        else:
            print(f"  Subscription verdict: at this run-rate, API would cost "
                  f"${-delta:,.2f}/mo LESS than your ${sub_cost:,.2f} plan "
                  f"-> API wins.")


def main():
    parser = argparse.ArgumentParser(description="Claude Code usage tracker")
    parser.add_argument("--scan-all", action="store_true",
                        help="Ingest every transcript under ~/.claude/projects")
    parser.add_argument("--report", action="store_true",
                        help="Print an ROI rollup from usage.csv")
    parser.add_argument("--month", help="Limit --report to a month (YYYY-MM)")
    parser.add_argument("--sub-cost", type=float,
                        help="Monthly subscription price for the breakeven verdict")
    args = parser.parse_args()

    if args.report:
        report(month=args.month, sub_cost=args.sub_cost)
    elif args.scan_all:
        count = ingest(sorted(glob.glob(PROJECTS_GLOB)))
        print(f"Ingested {count} new turn(s) into {CSV_PATH}")
    else:
        hook_mode()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never block a turn or surface a traceback to the hook runner.
        pass
    sys.exit(0)
