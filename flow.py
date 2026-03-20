"""
taskflow — local-first workflow engine
LLM for the new. Local logic for the known.
Points at Token Firewall (or any OpenAI-compatible endpoint) for LLM steps.
"""
import json
import os
import sys
import time
import requests

VERSION = "0.2.0"

# ── LLM config ────────────────────────────────────────────────────────────────
# Defaults point at Token Firewall running locally on port 8000.
# Override with environment variables if needed.
TASKFLOW_URL   = os.environ.get("TASKFLOW_URL",   "http://localhost:8000/v1")
TASKFLOW_KEY   = os.environ.get("TASKFLOW_KEY",   "taskflow")
TASKFLOW_MODEL = os.environ.get("TASKFLOW_MODEL", "default")

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
    '  summarize:     { "step": "summarize", "prompt": "instruction for the LLM" }\n'
    '  ask:           { "step": "ask", "prompt": "freeform question or instruction" }\n'
    '  webhook:       { "step": "webhook", "url": "..." }\n'
    '  write_file:    { "step": "write_file", "filename": "output.txt" }\n'
    "Return only a valid JSON array. No explanation. No markdown. No code fences."
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
    tokens  = body.get("usage", {}).get("total_tokens", 0) or 0
    return content.strip(), int(tokens)


# ── mechanical steps (zero tokens) ───────────────────────────────────────────

def run_fetch(step, data):
    url = step.get("url")
    if not url:
        raise ValueError("fetch step requires a 'url' field")
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return r.text


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


# ── dispatcher ────────────────────────────────────────────────────────────────

def run_step(step, data):
    kind = step.get("step")
    if kind == "fetch":          return run_fetch(step, data),          0
    if kind == "filter":         return run_filter(step, data),         0
    if kind == "extract_field":  return run_extract_field(step, data),  0
    if kind == "write_file":     return run_write_file(step, data),     0
    if kind == "webhook":        return run_webhook(step, data),        0
    if kind == "summarize":      return run_summarize(step, data)
    if kind == "ask":            return run_ask(step, data)
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
    confirm = input("\nrun this workflow? (y/n): ").strip().lower()
    if confirm != "y":
        print("aborted.")
        return
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
