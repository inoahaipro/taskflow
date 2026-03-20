"""
taskflow v0.4.0 — local-first workflow engine
LLM for the new. Local logic for the known.
Points at OpenClaw gateway for LLM steps.

Fetch resilience:
  1. Try the URL up to 2 times
  2. If both fail, ask the LLM for an alternative source and try that
  3. Only raises if everything fails
"""
import json
import os
import sys
import time
import requests

VERSION = "0.4.0"

# ── LLM config ────────────────────────────────────────────────────────────────
TASKFLOW_URL   = os.environ.get("TASKFLOW_URL",   "http://localhost:18789/v1")
TASKFLOW_KEY   = os.environ.get("TASKFLOW_KEY",   "b93525e070088a14ac01bc4d1ec3e16a7323961f23fc8ee5")
TASKFLOW_MODEL = os.environ.get("TASKFLOW_MODEL", "default")

# ── Telegram config (optional — for notify step) ──────────────────────────────
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID",   "")

# ── Known fallback chains (optional hints — engine also asks LLM dynamically) ─
FALLBACK_CHAINS = {
    "btc": [
        "https://api.coinbase.com/v2/prices/BTC-USD/spot",
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
        "https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD",
    ],
    "eth": [
        "https://api.coinbase.com/v2/prices/ETH-USD/spot",
        "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
    ],
}

# ── System prompts ────────────────────────────────────────────────────────────
SYSTEM_TRANSFORM = (
    "You are a data transformation assistant. "
    "You receive data and an instruction. "
    "Apply the instruction to the data and return the result as plain text. "
    "Be concise. No explanation. No markdown."
)

SYSTEM_FALLBACK_URL = (
    "You are a URL fallback assistant. "
    "A URL failed to fetch. Suggest one alternative public URL that returns "
    "the same or equivalent data as a raw JSON or text response. "
    "Return only the URL, nothing else. No explanation. No markdown."
)

SYSTEM_PLANNER = (
    "You are a workflow engine assistant. "
    "Convert the user's natural language description into a JSON array of workflow steps.\n\n"
    "Available step types:\n"
    '  fetch:         { "step": "fetch", "url": "...", "fallback_key": "btc" }\n'
    '  filter:        { "step": "filter", "contains": "..." }\n'
    '  extract_field: { "step": "extract_field", "field": "dot.separated.path" }\n'
    '  summarize:     { "step": "summarize", "prompt": "instruction for the LLM" }\n'
    '  ask:           { "step": "ask", "prompt": "question answered from LLM knowledge, no fetch needed" }\n'
    '  format:        { "step": "format", "mode": "json_to_text|flatten|list_to_csv" }\n'
    '  each:          { "step": "each", "substep": { "step": "summarize", "prompt": "..." } }\n'
    '  save_workflow: { "step": "save_workflow", "filename": "my_workflow.json" }\n'
    '  notify:        { "step": "notify", "message": "optional override message" }\n'
    '  write_file:    { "step": "write_file", "filename": "output.txt" }\n\n'
    "Rules:\n"
    "- For crypto prices use fetch with fallback_key 'btc' or 'eth'. Do NOT use coindesk URLs.\n"
    "- For general knowledge (history, facts, lists) use a single 'ask' step — no fetch needed.\n"
    "- For lists of items needing individual processing, use 'each'.\n"
    "- Always end with write_file, notify, or a summarize/ask that prints output.\n"
    "- Return only a valid JSON array. No explanation. No markdown. No code fences."
)


# ── LLM call ──────────────────────────────────────────────────────────────────

def call_llm(system, user):
    payload = {
        "model": TASKFLOW_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {TASKFLOW_KEY}",
        "Content-Type":  "application/json",
    }
    r = requests.post(
        f"{TASKFLOW_URL}/chat/completions",
        json=payload,
        headers=headers,
        timeout=60,
    )
    r.raise_for_status()
    body    = r.json()
    content = body["choices"][0]["message"]["content"]
    usage   = body.get("usage") or {}
    tokens  = usage.get("total_tokens") or usage.get("completion_tokens")
    if not tokens:
        tokens = max(1, len(content.split()) * 4 // 3)
    return content.strip(), int(tokens)


# ── smart fetch with 2-retry + LLM fallback ───────────────────────────────────

def _try_url(url):
    """Attempt a single GET. Returns parsed response or raises."""
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return r.text


def fetch_url(url, fallback_key=None):
    """
    Fetch strategy:
      1. Try the primary URL up to 2 times
      2. If a fallback_key chain exists, try those URLs once each
      3. Ask the LLM for an alternative URL and try that once
      4. Raise if everything failed
    """
    urls_tried = []

    # ── Stage 1: retry primary URL twice ─────────────────────────────────────
    for attempt in range(2):
        try:
            return _try_url(url)
        except Exception as e:
            last_err = e
            if attempt == 0:
                print(f"   ↩ fetch failed (attempt 1) — retrying...")
            else:
                print(f"   ↩ fetch failed (attempt 2) — trying fallbacks...")
    urls_tried.append(url)

    # ── Stage 2: known fallback chain (if provided) ───────────────────────────
    if fallback_key and fallback_key in FALLBACK_CHAINS:
        for fb_url in FALLBACK_CHAINS[fallback_key]:
            if fb_url in urls_tried:
                continue
            try:
                print(f"   ↩ trying chain fallback: {fb_url.split('/')[2]}...")
                result = _try_url(fb_url)
                print(f"   ✓ chain fallback succeeded")
                return result
            except Exception as e:
                last_err = e
                urls_tried.append(fb_url)

    # ── Stage 3: ask the LLM for an alternative URL ───────────────────────────
    try:
        print(f"   🧠 asking LLM for alternative source...")
        tried_str  = "\n".join(f"- {u}" for u in urls_tried)
        user_msg   = (
            f"This URL failed to fetch:\n{url}\n\n"
            f"Already tried:\n{tried_str}\n\n"
            "Suggest one alternative public API URL that returns equivalent data."
        )
        llm_url, _ = call_llm(SYSTEM_FALLBACK_URL, user_msg)
        llm_url    = llm_url.strip().strip('"').strip("'")
        if llm_url and llm_url.startswith("http") and llm_url not in urls_tried:
            print(f"   ↩ trying LLM suggestion: {llm_url.split('/')[2]}...")
            result = _try_url(llm_url)
            print(f"   ✓ LLM fallback succeeded")
            return result
    except Exception as e:
        last_err = e

    raise last_err


# ── mechanical steps (zero tokens) ───────────────────────────────────────────

def run_fetch(step, data):
    url          = step.get("url")
    fallback_key = step.get("fallback_key")
    if not url and fallback_key and fallback_key in FALLBACK_CHAINS:
        url = FALLBACK_CHAINS[fallback_key][0]
    if not url:
        raise ValueError("fetch step requires a 'url' or 'fallback_key' field")
    return fetch_url(url, fallback_key)


def run_filter(step, data):
    contains = step.get("contains", "").lower()
    if isinstance(data, list):
        return [item for item in data if contains in json.dumps(item).lower()]
    if isinstance(data, str):
        return [line for line in data.splitlines() if contains in line.lower()]
    return data


def run_extract_field(step, data):
    field  = step.get("field", "")
    result = data
    for key in field.split("."):
        if isinstance(result, dict):
            result = result.get(key)
        else:
            return None
    return result


def run_format(step, data):
    mode = step.get("mode", "json_to_text")
    if mode == "json_to_text":
        return json.dumps(data, indent=2) if not isinstance(data, str) else data
    if mode == "flatten":
        if isinstance(data, list):
            return [item for sublist in data for item in (sublist if isinstance(sublist, list) else [sublist])]
        return data
    if mode == "list_to_csv":
        if isinstance(data, list) and data and isinstance(data[0], dict):
            keys = list(data[0].keys())
            rows = [",".join(keys)]
            for item in data:
                rows.append(",".join(str(item.get(k, "")) for k in keys))
            return "\n".join(rows)
        return str(data)
    return data


def run_write_file(step, data):
    filename = step.get("filename", "output.txt")
    content  = data if isinstance(data, str) else json.dumps(data, indent=2)
    with open(filename, "w") as f:
        f.write(content)
    return f"written to {filename}"


def run_webhook(step, data):
    url = step.get("url")
    if not url:
        raise ValueError("webhook step requires a 'url' field")
    payload = {"content": data} if isinstance(data, str) else data
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    return f"webhook delivered · status {r.status_code}"


def run_notify(step, data):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return "notify: Telegram not configured (set TG_BOT_TOKEN + TG_CHAT_ID to enable)"
    message = step.get("message") or (data if isinstance(data, str) else json.dumps(data, indent=2))
    r = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": message},
        timeout=10,
    )
    r.raise_for_status()
    return "telegram message sent"


def run_save_workflow(step, data, steps_so_far):
    filename = step.get("filename", "saved_workflow.json")
    with open(filename, "w") as f:
        json.dump(steps_so_far, f, indent=2)
    return f"workflow saved to {filename}"


# ── LLM steps ─────────────────────────────────────────────────────────────────

def run_summarize(step, data):
    prompt   = step.get("prompt", "Summarize this.")
    data_str = data if isinstance(data, str) else json.dumps(data, indent=2)
    result, tokens = call_llm(SYSTEM_TRANSFORM, f"Instruction: {prompt}\nData: {data_str}")
    return result, tokens


def run_ask(step, data):
    prompt   = step.get("prompt", "")
    data_str = data if isinstance(data, str) else json.dumps(data, indent=2) if data else ""
    user     = f"{prompt}\n\nContext: {data_str}" if data_str else prompt
    result, tokens = call_llm(SYSTEM_TRANSFORM, user)
    return result, tokens


def run_each(step, data):
    substep = step.get("substep")
    if not substep:
        raise ValueError("each step requires a 'substep' field")
    if not isinstance(data, list):
        raise ValueError("each step requires list data from previous step")
    results      = []
    total_tokens = 0
    for i, item in enumerate(data):
        result, tokens = run_step(substep, item)
        total_tokens  += tokens
        results.append(result)
        print(f"   item {i+1}/{len(data)} · {tokens} tokens")
    return results, total_tokens


# ── dispatcher ────────────────────────────────────────────────────────────────

def run_step(step, data, all_steps=None):
    kind = step.get("step")
    if kind == "fetch":          return run_fetch(step, data),                    0
    if kind == "filter":         return run_filter(step, data),                   0
    if kind == "extract_field":  return run_extract_field(step, data),            0
    if kind == "format":         return run_format(step, data),                   0
    if kind == "write_file":     return run_write_file(step, data),               0
    if kind == "webhook":        return run_webhook(step, data),                  0
    if kind == "notify":         return run_notify(step, data),                   0
    if kind == "save_workflow":  return run_save_workflow(step, data, all_steps), 0
    if kind == "summarize":      return run_summarize(step, data)
    if kind == "ask":            return run_ask(step, data)
    if kind == "each":           return run_each(step, data)
    raise ValueError(f"unknown step type: '{kind}'")


# ── pipeline runner ───────────────────────────────────────────────────────────

def run_pipeline(steps, stop_on_error=True):
    data         = None
    trace        = []
    total_tokens = 0
    print(f"\n── taskflow v{VERSION} ──────────────────────────────")
    for i, step in enumerate(steps):
        kind  = step.get("step", "unknown")
        start = time.time()
        try:
            data, tokens  = run_step(step, data, all_steps=steps)
            elapsed        = round((time.time() - start) * 1000)
            total_tokens  += tokens if isinstance(tokens, int) else 0
            tok_label      = f"{tokens} tokens" if tokens else "0 tokens"
            trace.append({"step": kind, "status": "ok", "ms": elapsed, "tokens": tokens})
            print(f"✅ step {i+1} · {kind:<14} · {tok_label:<12} · {elapsed}ms")
        except Exception as e:
            elapsed = round((time.time() - start) * 1000)
            trace.append({"step": kind, "status": "error", "error": str(e)})
            print(f"❌ step {i+1} · {kind:<14} · ERROR: {e}")
            if stop_on_error:
                break
    failures = sum(1 for t in trace if t["status"] == "error")
    total_ms = sum(t["ms"] for t in trace if "ms" in t)
    print(f"────────────────────────────────────────────────────")
    print(f"total: {total_tokens} tokens · {total_ms}ms · {len(trace)} steps · {failures} failures\n")
    if data and isinstance(data, str):
        print(f"📄 output: {data}\n")
    elif data and isinstance(data, list):
        print(f"📄 output ({len(data)} items):")
        for item in data[:5]:
            print(f"   • {str(item)[:120]}")
        if len(data) > 5:
            print(f"   ... and {len(data) - 5} more")
        print()
    return data, trace


# ── natural language → workflow ───────────────────────────────────────────────

def ask_to_workflow(description):
    print(f"\n── taskflow v{VERSION} · planning ──────────────────")
    print(f"🧠 converting: \"{description}\"")
    raw, tokens = call_llm(SYSTEM_PLANNER, description)
    print(f"✅ plan generated · {tokens} tokens")
    try:
        steps = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(f"LLM returned invalid JSON:\n{raw}")
    print(f"📋 {len(steps)} steps planned:")
    for i, s in enumerate(steps):
        print(f"   {i+1}. {s.get('step', '?')}")
    # Auto-run without interactive confirmation so it can be driven non-interactively
    run_pipeline(steps)


# ── CLI ───────────────────────────────────────────────────────────────────────

def load_workflow(path):
    with open(path) as f:
        return json.load(f)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(f"taskflow v{VERSION}")
        print("usage:")
        print("  python flow.py run <workflow.json>    run a JSON workflow")
        print("  python flow.py ask \"<description>\"    describe in plain English")
        return

    if args[0] == "run" and len(args) >= 2:
        steps = load_workflow(args[1])
        run_pipeline(steps)
        return

    if args[0] == "ask" and len(args) >= 2:
        description = " ".join(args[1:])
        ask_to_workflow(description)
        return

    print(f"unknown command: {args[0]}")


if __name__ == "__main__":
    main()
