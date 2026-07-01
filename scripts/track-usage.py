#!/usr/bin/env python3
"""Claude Code token & cost usage tracker.

Logs one row per assistant turn for every Claude Code session to
~/.claude/usage/usage.csv, with an estimated API-equivalent cost per row, so you
can analyze spend in Excel and compare API cost against a subscription plan.

Modes:
  (default)        Hook mode. Reads hook JSON from stdin (Stop / SessionEnd),
                   locates the session transcript plus its nested subagent
                   transcripts, and ingests any new turns from all of them.
  --scan-all       Walk every ~/.claude/projects/**/*.jsonl (recursive) and
                   ingest. Backfills history and sweeps up nested subagent/team
                   transcript files.
  --report         Print an ROI rollup from usage.csv (no transcript parsing).
                   Options: --month YYYY-MM, --sub-cost N
  --reprice        Recompute the cost columns in usage.csv from the recorded
                   token counts using current pricing (backs up to
                   usage.csv.bak first). Use after prices change.
  --refresh-prices Fetch the latest public per-model prices into
                   ~/.claude/usage/prices.json.

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

Pricing is per model VERSION (Anthropic ties a price change to a new model, not a
repricing of an old one). A version-aware baseline table lives in this file as the
authoritative, offline, history-proof rate card; a cached overlay fetched from
Anthropic's public pricing page is merged on top so newly launched models get priced
without a code edit. When a never-before-seen model id is ingested, a price refresh is
attempted automatically (cooldown-gated) so the new model is costed correctly from its
first logged turn.

It always exits 0 and fails quietly, so it can never block a turn.
"""

import argparse
import csv
import datetime
import fcntl
import glob
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request

# --- Pricing: USD per 1,000,000 tokens. -------------------------------------
# BASELINE_PRICING is the authoritative, offline, history-proof rate card. Keys
# are matched as substrings against the full model id; the resolver tries the
# most specific (longest) key first, so version-specific keys win over family
# keys. Anthropic cut Opus pricing at the 4.5 launch — Opus 4.0/4.1 billed at
# 15/75, Opus 4.5+ at 5/25 — hence the version-specific Opus entries below.
#
# A cached overlay fetched from the public pricing page (~/.claude/usage/prices.json)
# is merged ON TOP of this baseline so newly launched models are priced without a
# code edit. The baseline always remains as the fallback when the cache is
# missing/stale or a fetch fails. List more specific keys first only matters for
# readability — the resolver sorts by key length regardless.
BASELINE_PRICING = [
    # key substring     input   output
    ("opus-4-1",        15.00, 75.00),   # Opus 4.1   (pre-4.5 pricing)
    ("opus-4-20250514", 15.00, 75.00),   # Opus 4.0   (dated id)
    ("opus-4-0",        15.00, 75.00),   # Opus 4.0
    ("opus",             5.00, 25.00),   # Opus 4.5+  (family fallback)
    # Sonnet 5's fallback here is its post-2026-08-31 standard rate; the $2/$10
    # introductory rate in effect until then is only ever available fetched.
    ("sonnet",           3.00, 15.00),   # all Sonnet 4.x + Sonnet 5 (family fallback)
    ("fable",           10.00, 50.00),   # Fable 5    (family fallback)
    ("mythos",          10.00, 50.00),   # Mythos 5   (same pricing as Fable 5)
    ("haiku-3-5",        0.80,  4.00),   # Haiku 3.5
    ("haiku",            1.00,  5.00),   # Haiku 4.5  (family fallback)
]
CACHE_READ_MULT = 0.10  # cache-read tokens billed at 0.1x the input rate
CACHE_5M_MULT = 1.25    # 5-minute ephemeral cache write
CACHE_1H_MULT = 2.00    # 1-hour ephemeral cache write

# Public pricing page (markdown). Parsed best-effort; any failure falls back to
# BASELINE_PRICING and is never fatal.
PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
# When a new model id appears in the hook path, don't re-attempt a fetch more
# often than this — catches a launch quickly without per-turn network calls.
NEW_MODEL_REFRESH_COOLDOWN = datetime.timedelta(hours=6)
# In --report, opportunistically refresh if the cache hasn't been tried this long.
PRICES_STALE_AFTER = datetime.timedelta(days=14)

USAGE_DIR = os.path.expanduser("~/.claude/usage")
CSV_PATH = os.path.join(USAGE_DIR, "usage.csv")
STATE_PATH = os.path.join(USAGE_DIR, ".state.json")
SEEN_PATH = os.path.join(USAGE_DIR, ".seen_ids")
SEEN_MODELS_PATH = os.path.join(USAGE_DIR, ".seen_models")
PRICES_PATH = os.path.join(USAGE_DIR, "prices.json")
LOCK_PATH = os.path.join(USAGE_DIR, ".lock")
# Wherever this script ends up installed, it records its own absolute path here so
# the slash command / CLI can find it without anyone hardcoding an install location.
SELF_PATH_FILE = os.path.join(USAGE_DIR, ".script_path")
# Recursive so subagent transcripts are swept up too. Claude Code writes the main
# session at ~/.claude/projects/<slug>/<session-id>.jsonl, but subagent/team turns
# (spawned at higher effort levels) land in nested dirs like
# <slug>/<session-id>/subagents/agent-*.jsonl and .../subagents/workflows/wf_*/agent-*.jsonl.
# `**` matches zero or more directories, so this still covers the top-level files.
# Safe to over-collect: ingest() dedupes globally on message.id (see .seen_ids).
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")

FIELDS = [
    "timestamp", "date", "session_id", "project", "cwd", "model",
    "input_tokens", "output_tokens", "cache_creation_5m_tokens",
    "cache_creation_1h_tokens", "cache_read_tokens", "total_tokens",
    "cost_usd", "cost_uncached_usd", "cc_version", "message_id",
]


# --- Price resolution -------------------------------------------------------

_resolved_pricing = None  # cached merged (baseline + fetched) match list


def load_fetched_prices():
    """Read the cached price overlay -> {key: (input, output)}. {} on any error."""
    try:
        with open(PRICES_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    out = {}
    for key, rate in (data.get("models") or {}).items():
        try:
            out[str(key)] = (float(rate["input"]), float(rate["output"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def build_pricing():
    """Merge baseline + fetched overlay into a longest-key-first match list."""
    merged = {key: (in_rate, out_rate) for key, in_rate, out_rate in BASELINE_PRICING}
    merged.update(load_fetched_prices())  # fetched rates win on key collision
    return [
        (key, rates[0], rates[1])
        for key, rates in sorted(merged.items(), key=lambda kv: len(kv[0]), reverse=True)
    ]


def resolved_pricing():
    global _resolved_pricing
    if _resolved_pricing is None:
        _resolved_pricing = build_pricing()
    return _resolved_pricing


def invalidate_pricing():
    global _resolved_pricing
    _resolved_pricing = None


def rates_for(model):
    for key, in_rate, out_rate in resolved_pricing():
        idx = model.find(key)
        if idx == -1:
            continue
        # Require a separator (or end of id) right after the key so a version key
        # like "opus-4-1" matches "opus-4-1-20250805" but NOT "opus-4-10"/"4-19",
        # which then correctly fall through to the family rate ("opus").
        nxt = idx + len(key)
        if nxt < len(model) and model[nxt] not in "-_.:":
            continue
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


def apply_cost(row):
    """Fill cost_usd / cost_uncached_usd on a row from its recorded token columns."""
    def _int(key):
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    cost, gross = compute_costs(
        row.get("model") or "",
        _int("input_tokens"), _int("output_tokens"), _int("cache_read_tokens"),
        _int("cache_creation_5m_tokens"), _int("cache_creation_1h_tokens"),
    )
    row["cost_usd"] = cost
    row["cost_uncached_usd"] = gross
    return row


# --- Price refresh (fetch the public pricing page) --------------------------

def _model_key(text):
    """Map a pricing-table display name to a version-specific id substring, e.g.
    'Claude Opus 4.8' -> 'opus-4-8', 'Claude Sonnet 5' -> 'sonnet-5'.

    Anthropic has used both a dotted minor version (Opus, Haiku, and Sonnet through
    4.6) and a bare integer (Fable, Mythos, and Sonnet from 5 onward) as the current
    generation's version scheme, so both must match. A bare major version is only
    safe to treat as a real key once retired/deprecated rows are filtered out by the
    caller — otherwise e.g. 'Claude Opus 4 (retired...)' would yield the over-broad
    key 'opus-4', matching 'opus-4-10' etc. at the wrong (old) price."""
    m = re.search(r"Claude\s+(Opus|Sonnet|Haiku|Fable|Mythos)\s+(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    return f"{m.group(1).lower()}-{m.group(2).replace('.', '-')}"


def _row_date_qualifier(line):
    """Extract a '[through DATE]' / 'starting DATE' qualifier from a pricing-table
    row, e.g. Anthropic listing a model's introductory rate and its later standard
    rate as two separate rows for the same model. Returns ('through'|'starting',
    date) or None."""
    m = re.search(r"(through|starting)\s+([A-Za-z]+ \d{1,2}, \d{4})", line)
    if not m:
        return None
    try:
        return m.group(1), datetime.datetime.strptime(m.group(2), "%B %d, %Y").date()
    except ValueError:
        return None


def _row_is_active(qualifier, today):
    if qualifier is None:
        return True
    kind, date = qualifier
    return today <= date if kind == "through" else today >= date


def _parse_price_table(md):
    """Extract {key: {'input':, 'output':}} from the page's 'Model pricing' table.

    Scoped to that one section so the discounted Batch / Fast-mode tables (same
    model names, different numbers) don't clobber the standard rates. Retired and
    deprecated rows are skipped — their price is superseded and continues to be
    served by the hand-curated baseline, so keeping them out here avoids the same
    bare-major-key over-match _model_key warns about. When a model has multiple
    active rows (e.g. a time-boxed introductory rate alongside its later standard
    rate), the row whose date qualifier covers today wins, regardless of table
    order."""
    start = md.find("# Model pricing")
    if start == -1:
        start = md.find("Model pricing")
    section = md[start:] if start != -1 else md
    nxt = re.search(r"\n#{1,6} ", section[1:])  # stop at the next heading, any level
    if nxt:
        section = section[: nxt.start() + 1]
    rows_by_key = {}
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        if re.search(r"retired|deprecated", line, re.IGNORECASE):
            continue
        key = _model_key(line)
        if not key:
            continue
        amounts = re.findall(r"\$\s*([\d.]+)\s*/\s*MTok", line)
        if len(amounts) < 2:
            continue
        try:
            price = {"input": float(amounts[0]), "output": float(amounts[-1])}
        except ValueError:
            continue
        rows_by_key.setdefault(key, []).append((_row_date_qualifier(line), price))

    today = datetime.datetime.now(datetime.timezone.utc).date()
    prices = {}
    for key, rows in rows_by_key.items():
        active = [price for qualifier, price in rows if _row_is_active(qualifier, today)]
        prices[key] = active[-1] if active else rows[-1][1]
    return prices


def refresh_prices():
    """Fetch the public pricing page and refresh the cached overlay.

    Best-effort: always records the attempt time (so the cooldown holds even when
    offline), never raises, and preserves prior models on a failed fetch. Returns
    the model count on a successful parse, else None."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with open(PRICES_PATH) as f:
            cache = json.load(f)
        if not isinstance(cache, dict):
            cache = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        cache = {}
    cache["attempted_at"] = now

    models = None
    try:
        req = urllib.request.Request(
            PRICING_URL, headers={"User-Agent": "claude-code-usage-tracking"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            md = resp.read().decode("utf-8", "replace")
        parsed = _parse_price_table(md)
        if parsed:
            models = parsed
    except (urllib.error.URLError, OSError, ValueError):
        models = None

    if models:
        cache["models"] = models
        cache["fetched_at"] = now
        cache["source"] = PRICING_URL
    try:
        os.makedirs(USAGE_DIR, exist_ok=True)
        tmp = PRICES_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp, PRICES_PATH)
    except OSError:
        pass
    invalidate_pricing()
    return len(models) if models else None


def prices_meta():
    """Return the cached price metadata dict, or None if no cache exists."""
    try:
        with open(PRICES_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _attempted_within(window):
    """True if a refresh was last attempted less than `window` ago."""
    meta = prices_meta() or {}
    raw = meta.get("attempted_at")
    if not isinstance(raw, str):
        return False
    try:
        last = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - last) < window


def row_from_record(d):
    """Build a CSV row dict (cost filled later) from a transcript line, or None."""
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
        "cost_usd": "",          # filled by apply_cost after any price refresh
        "cost_uncached_usd": "",
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


def load_seen_models():
    """Set of model ids ever ingested — used to detect never-before-seen models."""
    try:
        with open(SEEN_MODELS_PATH) as f:
            return {line.strip() for line in f if line.strip()}
    except (FileNotFoundError, OSError):
        return set()


def append_seen_models(models):
    if not models:
        return
    try:
        with open(SEEN_MODELS_PATH, "a") as f:
            for model in sorted(models):
                f.write(model + "\n")
    except OSError:
        pass


def record_self():
    """Record this script's absolute path so the CLI/slash command can locate it
    regardless of where the plugin was installed (no hardcoded paths anywhere)."""
    try:
        os.makedirs(USAGE_DIR, exist_ok=True)
        with open(SELF_PATH_FILE, "w") as f:
            f.write(os.path.abspath(__file__))
    except OSError:
        pass


def parse_rows(path, state, seen, seen_fh):
    """Parse new (offset-onward) complete lines from one transcript into row dicts
    (cost filled later), advancing the byte offset and the seen-id set."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return []
    offset = state.get(path, 0)
    size = os.path.getsize(path)
    if offset > size:  # file shrank / rotated — re-read from the start
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    last_nl = data.rfind(b"\n")
    if last_nl == -1:  # no complete line yet
        return []
    consumed = data[: last_nl + 1]
    rows = []
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
        rows.append(row)
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
        rows = []
        with open(SEEN_PATH, "a") as sf:
            for p in paths:
                rows.extend(parse_rows(p, state, seen, sf))

        if rows:
            # A never-before-seen model id likely means a new model launched, so
            # refresh prices once (cooldown-gated) before costing — this turn's
            # rows then get the correct rate from the start. Fully guarded.
            new_models = {r["model"] for r in rows if r.get("model")} - load_seen_models()
            if new_models:
                append_seen_models(new_models)
                if not _attempted_within(NEW_MODEL_REFRESH_COOLDOWN):
                    refresh_prices()
            for r in rows:
                apply_cost(r)
            with open(CSV_PATH, "a", newline="") as cf:
                writer = csv.DictWriter(cf, fieldnames=FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerows(rows)

        save_state(state)
        return len(rows)
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


def session_transcripts(main_path):
    """Given a session's main transcript path, return it plus any nested subagent
    transcripts for that same session. Claude Code writes subagent/team turns
    (spawned at higher effort levels) under <session-id>/subagents/.../*.jsonl,
    a sibling subtree of the main <session-id>.jsonl file. Sweeping it on every
    Stop/SessionEnd keeps subagent spend current; ingest() reads only new bytes
    per file and dedupes globally on message.id, so re-running is cheap and safe."""
    paths = [main_path]
    if main_path.endswith(".jsonl"):
        session_dir = main_path[: -len(".jsonl")]
        paths.extend(glob.glob(os.path.join(session_dir, "**", "*.jsonl"),
                               recursive=True))
    return paths


def hook_mode():
    try:
        hook = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    path = resolve_transcript(hook)
    if path:
        ingest(sorted(session_transcripts(path)))


def reprice():
    """Recompute the cost columns in usage.csv from the recorded token counts and
    current pricing. Backs up to usage.csv.bak first. Safe and idempotent."""
    if not os.path.exists(CSV_PATH):
        print(f"No usage data at {CSV_PATH}")
        return
    # Hold the same lock the hook uses so a concurrent ingest can't have its
    # just-appended row clobbered by our atomic rewrite.
    os.makedirs(USAGE_DIR, exist_ok=True)
    lock_fh = open(LOCK_PATH, "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    try:
        with open(CSV_PATH, newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or FIELDS
            rows = list(reader)
        before = sum(_f(r, "cost_usd") for r in rows)
        changed = 0
        for r in rows:
            old = str(r.get("cost_usd"))
            apply_cost(r)
            if str(r.get("cost_usd")) != old:
                changed += 1
        after = sum(_f(r, "cost_usd") for r in rows)

        backup = CSV_PATH + ".bak"
        shutil.copy2(CSV_PATH, backup)
        tmp = CSV_PATH + ".tmp"
        with open(tmp, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, CSV_PATH)

        # Every model already in the CSV has been "seen", so record them — the
        # hook's new-model auto-refresh then fires only for genuinely new models.
        append_seen_models({r["model"] for r in rows if r.get("model")} - load_seen_models())
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()

    print(f"Repriced {len(rows)} row(s); {changed} changed.")
    print(f"  Total actual cost: ${before:,.2f} -> ${after:,.2f}")
    print(f"  Backup written to: {backup}")


def _f(row, key):
    try:
        return float(row[key]) if row[key] else 0.0
    except (ValueError, KeyError):
        return 0.0


def report(month=None, sub_cost=None):
    if not os.path.exists(CSV_PATH):
        print(f"No usage data yet at {CSV_PATH}")
        return
    # Keep the price cache reasonably fresh (out of the hook path). Best-effort.
    if not _attempted_within(PRICES_STALE_AFTER):
        refresh_prices()
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
    meta = prices_meta()
    if meta and meta.get("models") and meta.get("fetched_at"):
        price_src = f"baseline + fetched {str(meta['fetched_at'])[:10]} ({len(meta['models'])} models)"
    else:
        price_src = "baseline only"

    print(f"Claude Code usage report ({scope})")
    print(f"  Date span:           {span}  ({days} active day(s))")
    print(f"  Sessions:            {len(sessions)}")
    print(f"  Turns logged:        {turns}")
    print(f"  Prices:              {price_src}")
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
    parser.add_argument("--reprice", action="store_true",
                        help="Recompute usage.csv cost columns with current pricing")
    parser.add_argument("--refresh-prices", action="store_true",
                        help="Fetch the latest public per-model prices into the cache")
    args = parser.parse_args()

    record_self()
    if args.refresh_prices:
        count = refresh_prices()
        if count:
            print(f"Refreshed prices for {count} model(s) -> {PRICES_PATH}")
        else:
            print("Price refresh failed (offline or page format changed); "
                  "baseline pricing still in effect.")
    elif args.reprice:
        reprice()
    elif args.report:
        report(month=args.month, sub_cost=args.sub_cost)
    elif args.scan_all:
        count = ingest(sorted(glob.glob(PROJECTS_GLOB, recursive=True)))
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
