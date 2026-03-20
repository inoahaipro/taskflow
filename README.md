# taskflow

Local‑first workflow engine.

Describe a pipeline in plain English or JSON, execute it step by step, and
only spend tokens where logic is actually needed.

---

## What it is

`taskflow` is a tiny workflow runner that sits on top of an OpenAI‑compatible
endpoint (like **Token Firewall**) and gives you two things:

- **Mechanical, zero‑token steps** for the boring bits:
  - `fetch`          → GET a URL and parse JSON/text
  - `filter`         → filter lists/strings by substring
  - `extract_field`  → follow a `dot.separated.path` in JSON
  - `write_file`     → write the current result to disk
  - `webhook`        → POST the result to a URL
- **LLM‑powered steps** only where needed:
  - `summarize`      → summarise / transform a payload
  - `ask`            → arbitrary question/transform with optional context

Everything runs locally as a simple Python script.

---

## How it talks to the LLM

By default, `taskflow` points at a local **Token Firewall** instance:

```python
TASKFLOW_URL   = os.environ.get("TASKFLOW_URL",   "http://localhost:8000/v1")
TASKFLOW_KEY   = os.environ.get("TASKFLOW_KEY",   "taskflow")
TASKFLOW_MODEL = os.environ.get("TASKFLOW_MODEL", "default")
```

You can override these env vars to point at any OpenAI‑compatible endpoint:

```bash
export TASKFLOW_URL="https://api.openai.com/v1"
export TASKFLOW_KEY="sk-..."
export TASKFLOW_MODEL="gpt-4.1-mini"
```

Under the hood it calls `/chat/completions` with a small system prompt for
transformations and a separate one for planning.

---

## Step types

Planner output and JSON workflows use a very small vocabulary:

```json
[
  { "step": "fetch",        "url": "https://api.example.com/data" },
  { "step": "filter",       "contains": "bitcoin" },
  { "step": "extract_field", "field": "price.usd" },
  { "step": "summarize",    "prompt": "Explain this price movement." },
  { "step": "write_file",   "filename": "report.txt" }
]
```

Supported `step` values:

- `fetch`         — HTTP GET
- `filter`        — filter JSON/lines by substring
- `extract_field` — dot‑path lookup into nested JSON
- `summarize`     — LLM transform with an instruction
- `ask`           — freeform LLM question/transform
- `webhook`       — POST result to a URL
- `write_file`    — dump the result to a file

---

## CLI usage

Install deps:

```bash
pip install -r requirements.txt
```

### Run a JSON workflow

```bash
python flow.py run workflow.json
```

`workflow.json` should be a JSON array of step objects. Every step sees the
result of the previous step as its input.

### Plan from natural language

```bash
python flow.py ask "Fetch the current BTC price in USD and summarize it in one sentence"
```

This will:

1. Call the LLM with a planning system prompt to convert your description into
   a JSON step list.
2. Print the steps and ask:

   ```text
   run this workflow? (y/n):
   ```

3. If you confirm, execute the steps locally, printing a trace like:

   ```text
   ✅ step 1 · fetch          · 0 tokens     · 133ms
   ✅ step 2 · extract_field  · 0 tokens     · 2ms
   ✅ step 3 · summarize      · 58 tokens    · 820ms
   total: 58 tokens · 955ms · 3 steps · 0 failures
   ```

The goal is to keep all the HTTP + JSON plumbing local and cheap, and only
spend tokens when you really need the model.

---

## Relationship to Token Firewall

`taskflow` is happiest when you point it at **Token Firewall** instead of a
raw model:

- Firewall handles caching, safety, and device actions.
- Taskflow handles coordination: fetch / filter / extract / summarize / send.

Together they give you:

- Local‑first workflows.
- 0 tokens on cache hits (thanks to Token Firewall).
- Clear traces of where tokens were actually spent.

You can also point taskflow at any other OpenAI‑compatible server if you just
want a lightweight workflow runner.

---

## Status

- Early prototype (`VERSION = "0.2.0"` in `flow.py`).
- API and step vocabulary may grow, but the core idea is stable:
  **LLM for the new, local logic for the known.**
