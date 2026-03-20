"""
taskflow v0.3.0 — local-first workflow engine
LLM for the new. Local logic for the known.
Points at OpenClaw gateway for LLM steps.
"""
import json
import os
import sys
import time
import requests

VERSION = "0.3.0"

# ── OpenClaw config ───────────────────────────────────────────────────────────
TASKFLOW_URL   = os.environ.get("TASKFLOW_URL",   "http://localhost:18789/v1")
TASKFLOW_KEY   = os.environ.get("TASKFLOW_KEY",   "b93525e070088a14ac01bc4d1ec3e16a7323961f23fc8ee5")
TASKFLOW_MODEL = os.environ.get("TASKFLOW_MODEL", "default")

# ── Telegram config (optional — for notify step) ──────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN",  "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT",   "")

# ── Fallback URL chains ───────────────────────────────────────────────────────
# If a fetch fails, the engine walks this list and retries automatically.
# Key is a keyword the planner might put in a URL, value is ordered fallbacks.
FALLBACK_CHAINS = {
    "btc":      [
        "https://api.coinbase.com/v2/prices/BTC-USD/spot",
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
    ],
    "bitcoin":  [
        "https://api.coinbase.com/v2/prices/BTC-USD/spot",
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
    ],
    "coindesk": [
        "https://api.coinbase.com/v2/prices/BTC-USD/spot",
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
    ],
    "ethereum": [
        "https://api.coinbase.com/v2/prices/ETH-USD/spot",
        "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
    ],
    "eth":      [
        "https://api.coinbase.com/v2/prices/ETH-USD/spot",
        "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
    ],
}

# ── Saved workflows dir ───────────────────────────────────────────────────────
WORKFLOWS_DIR = os.environ.get("TASKFLOW_WORKFLOWS_DIR", "workflows")

# ── System prompts ────────────────────────────────────────────────────────────
SYSTEM_TRANSFORM = (
    "You are a data transformation assistant. "
    "You receive data and an instruction. "
    "Apply the instruction to the data and return the result as plain text. "
    "Be concise. No explanation. No markdown."
)

SYSTEM_PLANNER = (
    "You are a workflow engine assistant. "
    "Convert the user's natural language description into a JSON array of workflow steps.\n"
    "Available step types:\n"
    '  fetch:         { "step": "fetch", "url": "..." }\n'
    '  filter:        { "step": "filter", "contains": "..." }\n'
    '  extract_field: { "step": "extract_field", "field": "dot.separated.path" }\n'
    '  format:        { "step": "format", "mode": "csv|flatten|keys|count" }\n'
    '  summarize:     { "step": "summarize", "prompt": "instruction for the LLM" }\n'
    '  ask:           { "step": "ask", "prompt": "answer this from your own knowledge, no fetch needed" }\n'
    '  each:          { "step": "each", "substep": { "step": "summarize", "prompt": "..." } }\n'
    '  write_file:    { "step": "write_file", "filename": "output.txt" }\n'
    '  webhook:       { "step": "webhook", "url": "..." }\n'
    "Return only a valid JSON array. No explanation. No markdown. No code fences.\n"
    "Important rules:\n"
    "- For crypto prices use: https://api.coinbase.com/v2/prices/BTC-USD/spot (swap BTC for other coins)\n"
    "- Never use coindesk.com — it is dead.\n"
    "- For questions the LLM can answer from knowledge (history, facts, math), use a single 'ask' step with no fetch.\n"
    "- For Wikipedia or news pages that might block bots, prefer 'ask' over 'fetch'.\n"
    "- Only fetch URLs when live/real-time data is genuinely needed.\n"
)


# ── Token estimation fallback ─────────────────────────────────────────────────

def estimate_tokens(text):
    """Rough estimate: ~4 chars per token. Used when API returns no usage field."""
    return max(1, len(text) // 4)


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
    # Use real token count if available, otherwise estimate from response length
    usage   = body.get("usage") or {}
    tokens  = usage.get("total_tokens") or estimate_tokens(content)
    return content.strip(), int(tokens)


# ── URL fallback fetch ────────────────────────────────────────────────────────

def fetch_with_fallback(url):
    """Try the given URL. If it fails, check FALLBACK_CHAINS for alternatives."""
    urls_to_try = [url]
    url_lower = url.lower()
    for keyword, fallbacks in FALLBACK_CHAINS.items():
        if keyword in url_lower:
            for fb in fallbacks:
                if fb not in urls_to_try:
                    urls_to_try.append(fb)
            break

    last_err = None
    for attempt_url in urls_to_try:
        try:
            if attempt_url != url:
                print(f"   ↳ retrying with fallback: {attempt_url}")
            r = requests.get(attempt_url, timeout=10)
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return r.text
        except Exception as e:
            last_err = e
            continue
    raise last_err


# ── mechanical steps (zero tokens) ───────────────────────────────────────────

def run_fetch(step, data):
    url = step.get("url")
    if not url:
        raise ValueError("fetch step requires a 'url' field")
    return fetch_with_fallback(url)


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
    mode = step.get("mode", "flatten")
    if mode == "count":
        if isinstance(data, list):
            return str(len(data))
        if isinstance(data, str):
            return str(len(data.splitlines()))
        return "1"
    if mode == "keys":
        if isinstance(data, dict):
            return list(data.keys())
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return list(data[0].keys())
        return data
    if mode == "csv":
        if isinstance(data, list) and data and isinstance(data[0], dict):
            keys = list(data[0].keys())
            lines = [",".join(keys)]
            for row in data:
                lines.append(",".join(str(row.get(k, "")) for k in keys))
            return "\n".join(lines)
        return str(data)
    if mode == "flatten":
        if isinstance(data, (dict, list)):
            return json.dumps(data, indent=2)
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
    """Send output to Telegram. Set TELEGRAM_TOKEN and TELEGRAM_CHAT to enable."""
    token = TELEGRAM_TOKEN or step.get("token", "")
    chat  = TELEGRAM_CHAT  or step.get("chat",  "")
    if not token or not chat:
        return "notify: TELEGRAM_TOKEN and TELEGRAM_CHAT not set — skipped"
    text = data if isinstance(data, str) else json.dumps(data, indent=2)
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text},
        timeout=10,
    )
    r.raise_for_status()
    return f"telegram message sent · chat {chat}"


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
    """Run a substep on every item in a list."""
    substep = step.get("substep")
    if not substep:
        raise ValueError("each step requires a 'substep' field")
    if not isinstance(data, list):
        raise ValueError("each step requires list data from previous step")
    results      = []
    total_tokens = 0
    for item in data:
        result, tokens = run_step(substep, item)
        results.append(result)
        total_tokens += tokens
    return results, total_tokens


# ── dispatcher ────────────────────────────────────────────────────────────────

def run_step(step, data):
    kind = step.get("step")
    if kind == "fetch":         return run_fetch(step, data),         0
    if kind == "filter":        return run_filter(step, data),        0
    if kind == "extract_field": return run_extract_field(step, data), 0
    if kind == "format":        return run_format(step, data),        0
    if kind == "write_file":    return run_write_file(step, data),    0
    if kind == "webhook":       return run_webhook(step, data),       0
    if kind == "notify":        return run_notify(step, data),        0
    if kind == "summarize":     return run_summarize(step, data)
    if kind == "ask":           return run_ask(step, data)
    if kind == "each":          return run_each(step, data)
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
            data, tokens  = run_step(step, data)
            elapsed        = round((time.time() - start) * 1000)
            total_tokens  += tokens
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
        for item in data[:10]:
            print(f"   • {item}")
        if len(data) > 10:
            print(f"   ... and {len(data) - 10} more")
        print()
    return data, trace


# ── save / load workflows ─────────────────────────────────────────────────────

def save_workflow(name, steps):
    os.makedirs(WORKFLOWS_DIR, exist_ok=True)
    safe_name = name.lower().replace(" ", "_").replace("/", "_")[:40]
    path = os.path.join(WORKFLOWS_DIR, f"{safe_name}.json")
    with open(path, "w") as f:
        json.dump(steps, f, indent=2)
    print(f"💾 saved to {path}")
    return path


def load_workflow(path):
    with open(path) as f:
        return json.load(f)


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
    confirm = input("\nrun this workflow? (y/n): ").strip().lower()
    if confirm != "y":
        print("aborted.")
        return None, []
    data, trace = run_pipeline(steps)
    failures = sum(1 for t in trace if t["status"] == "error")
    if not failures:
        save = input("save this workflow for reuse? (y/n): ").strip().lower()
        if save == "y":
            save_workflow(description[:40], steps)
    return data, trace


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(f"taskflow v{VERSION}")
        print("usage:")
        print("  python flow.py run <workflow.json>    run a saved JSON workflow")
        print("  python flow.py ask \"<description>\"    describe in plain English")
        print("  python flow.py list                   list saved workflows")
        return

    if args[0] == "run" and len(args) >= 2:
        steps = load_workflow(args[1])
        run_pipeline(steps)
        return

    if args[0] == "ask" and len(args) >= 2:
        description = " ".join(args[1:])
        ask_to_workflow(description)
        return

    if args[0] == "list":
        os.makedirs(WORKFLOWS_DIR, exist_ok=True)
        files = [f for f in os.listdir(WORKFLOWS_DIR) if f.endswith(".json")]
        if not files:
            print("no saved workflows yet.")
        else:
            print(f"saved workflows in {WORKFLOWS_DIR}/:")
            for f in sorted(files):
                print(f"  {f}")
        return

    print(f"unknown command: {args[0]}")


if __name__ == "__main__":
    main()
