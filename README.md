# Assignment 9 — Multi-Agent AI with Browser Automation & Self-Healing Orchestration

A production-grade multi-agent AI system that plans, browses the web, distills facts, critiques its own output, and auto-recovers from failures — all without human intervention.

---

## Architecture Overview

```
User Query
    │
    ▼
┌─────────┐     DAG of skill nodes
│ Planner │ ──► Browser → Distiller → [Critic] → Formatter
└─────────┘                                │
                                  critic fail?
                                           │
                              ┌────────────▼──────────────┐
                              │  Recovery Planner (spliced │
                              │  into live graph)          │
                              └───────────────────────────┘
```

The **Planner** emits the full execution graph (DAG) upfront. Nodes run in parallel wherever possible. A **Critic** is auto-inserted after every distiller output to verify accuracy before the answer reaches the user. On critic failure, a new Planner is spliced into the live graph to replan from remaining work.

---

## Key Components

### Browser Skill — 4-Layer Cascade

```
Layer 1  →  HTML extract via trafilatura       (no LLM, pure speed)
Layer 2a →  Deterministic CSS selectors        (if metadata.selectors provided)
Layer 2b →  Accessibility Driver (A11y)        (reads rendered DOM)
Layer 3  →  Vision Driver (Set-of-Marks)       (screenshot-based)
```

Each layer escalates to the next only if the previous one fails. On any listing page the browser also:
- Captures a **ranked listing context** from card link text (model name + sort metric)
- Follows the **top N detail pages** for richer per-item data

### Skills Pipeline

| Skill | Role |
|---|---|
| `planner` | Emits the DAG of skill nodes for the query |
| `browser` | 4-layer web extraction + detail page following |
| `distiller` | Extracts structured fields from raw browser content |
| `critic` | Verifies distiller output — returns `pass` or `fail` with rationale |
| `formatter` | Renders the final human-readable answer |

### Recovery & Self-Healing

- **Critic fail** → recovery Planner is inserted; re-runs the browser + distiller branch
- **Node failure** → classified as `transient`, `validation_error`, or `upstream_failure`
- **Upstream failure** → `plan_recovery` queues a new Planner node with a failure report

---

## How to Run

### Prerequisites

```bash
# Set up environments (do once)
cd llm_gatewayV9
uv sync

cd ../S9SharedCode/code
uv sync
```

### Running a Query

```bash
cd S9SharedCode/code
uv run python flow.py "your query here"
```

---

## Sample Runs

### Query 1 — HuggingFace Model Comparison

```
uv run python flow.py "Compare top 3 Hugging Face text-generation models sorted by likes."
```

```
══════════════════════════════════════════════════════════════════════════════
session s8-4590da7b  ─  query: Compare top 3 Hugging Face text-generation models sorted by likes.
══════════════════════════════════════════════════════════════════════════════
[memory.read] 8 hit(s) visible to every skill this run
[n:1] planner            complete (4.6s)
[browser] ── start ──────────────────────────────────────
[browser] url       : https://huggingface.co/models
[browser] goal      : Filter by Task: Text Generation, Sort by: Most Likes; then extract the names, descriptions, and like counts of the top 3 models.
[browser] layer1: fetch ok — 331123 chars, final_url=https://huggingface.co/models
[browser] layer1: extract → 1416 chars, is_useful=False
[browser] layer1: extract insufficient — escalating
[browser] layer2b: running A11yDriver ...
[browser._drive] A11yDriver: driver.run() done — success=True
[browser._drive] A11yDriver: listing mode — skipping thin trafilatura (1605 chars)
[browser] _follow_detail_pages: ranking header:
RANKED LISTING FROM PAGE (sorted by: likes):
  #1: https://huggingface.co/deepseek-ai/DeepSeek-R1  [likes=13.4k]
  #2: https://huggingface.co/meta-llama/Meta-Llama-3-8B  [likes=6.57k]
  #3: https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct  [likes=6.05k]
[browser] _follow_detail_pages: visited 3 detail pages → 8503 chars
[n:2] browser            complete (19.8s)
[n:3] distiller          complete (1.4s)
  [debug:distiller] output = {
    "model_1": "deepseek-ai/DeepSeek-R1 (13.4k likes)",
    "model_2": "meta-llama/Meta-Llama-3-8B (6.57k likes)",
    "model_3": "meta-llama/Llama-3.1-8B-Instruct (6.05k likes)"
  }
[n:5] critic             complete (0.9s)
  [debug:critic] verdict   = pass
  [debug:critic] rationale = All required fields are present and the claims are consistent with the provided upstream output.
[n:4] formatter          complete (3.0s)
```

**Final Answer:**
```
As of the latest data, the top 3 text-generation models on Hugging Face,
ranked by their number of likes, are as follows:

1. deepseek-ai/DeepSeek-R1: 13.4k likes
2. meta-llama/Meta-Llama-3-8B: 6.57k likes
3. meta-llama/Llama-3.1-8B-Instruct: 6.05k likes

The clear leader is deepseek-ai/DeepSeek-R1, which holds significantly
more likes than the other two models in the top three.
```

---

### Query 2 — Amazon Laptop Comparison

```
uv run python flow.py "Compare 3 laptops under ₹80,000 on amazon.in"
```

```
══════════════════════════════════════════════════════════════════════════════
session s8-f13411b2  ─  query: Compare 3 laptops under ₹80,000 on amazon.in
══════════════════════════════════════════════════════════════════════════════
[memory.read] 8 hit(s) visible to every skill this run
[n:1] planner            complete (4.7s)
[browser] ── start ──────────────────────────────────────
[browser] url       : https://www.amazon.in/s?k=laptops+under+80000
[browser] layer1: fetch ok — 1411159 chars
[browser] layer2b: running A11yDriver ...
[browser._drive] A11yDriver: listing mode — using trafilatura (5407 chars → capped at 5000)
[browser._drive] A11yDriver: detail pages added 5237 chars — total extracted now 10239 chars
[n:2] browser            complete (26.4s)
[n:3] distiller          complete (1.5s)
[n:5] critic             complete (1.1s)
  [debug:critic] verdict   = fail   ← critic caught fabricated data, triggers recovery
  ↪ critic-fail recovery: planner node n:6 for n:3
[n:6] planner            complete (3.1s)   ← recovery planner replans
[n:7] browser            complete (15.6s)  ← second browser run with better goal
[n:8] distiller          complete (1.6s)
  [debug:distiller] output = {
    "laptop_1": {"model": "Lenovo Ideapad Slim 3", "price": "₹76,990", "specs": "13th Gen Intel Core i7 13620H, 16GB RAM, 512GB SSD, WUXGA"},
    "laptop_2": {"model": "ASUS TUF A15 (2025)",   "price": "₹68,990", "specs": "AMD Ryzen 7 7445HS, RTX 3050-4GB, 16GB DDR5"},
    "laptop_3": {"model": "Dell G15",               "price": "₹79,490", "specs": "13th Gen Intel Core i5-13450HX, NVIDIA RTX 3050-6GB, 16GB RAM"}
  }
[n:10] critic            complete (1.0s)
  [debug:critic] verdict   = pass
  [debug:critic] rationale = All three laptops are priced under ₹80,000 with specifications present.
[n:9] formatter          complete (2.9s)
```

**Final Answer:**

| Feature | Lenovo Ideapad Slim 3 | ASUS TUF A15 (2025) | Dell G15 |
|:---|:---|:---|:---|
| Price | ₹76,990 | ₹68,990 | ₹79,490 |
| Processor | 13th Gen Intel Core i7 13620H | AMD Ryzen 7 7445HS | 13th Gen Intel Core i5-13450HX |
| Graphics | Integrated | NVIDIA RTX 3050 (4GB) | NVIDIA RTX 3050 (6GB) |
| Memory | 16GB RAM | 16GB DDR5 | 16GB RAM |
| Storage | 512GB SSD | Not specified | Not specified |
| Display | WUXGA | Not specified | Not specified |

---

## Engineering Highlights

### Problem: React/SPA pages (HuggingFace)
Trafilatura extracted only ~1600 chars of boilerplate from a 331K React-rendered page. Distiller hallucinated model names from parametric memory.

**Fix:** Capture each model card's `innerText` as a structured ranked listing. Extract the last formatted number with a magnitude suffix (k/M/B) from the card text — this is always the sort metric on listing pages. Label it explicitly using the URL's `sort=` parameter.

### Problem: E-commerce nav links polluting detail page selection (Amazon)
Amazon has no `<main>` element. The full-body fallback selected header nav links (`/ap/signin`, `/gp/cart`, `/baby-reg/homepage`) before any product links.

**Fix (two-layer):**
1. **Path-segment keyword filter** — checks ALL path segments (not just first) against auth/transactional keywords (`signin`, `cart`, `order-history`, etc.)
2. **Text-length floor of 25 chars** — nav flyout items always have short text; product card links always include the full product name + price + specs

### Problem: Distiller confusing download count with like count
HuggingFace cards show two numbers: `5.35M` (downloads) and `13.4k` (likes). Distiller picked the larger number as "likes", producing an unsorted result that the critic correctly rejected.

**Fix:** Regex `r'\d+(?:\.\d+)?[kKmMbBtT]'` extracts all magnitude-suffixed numbers. The last one is consistently the sort metric. Combined with the `sort=likes` URL param, it's labeled as `[likes=13.4k]` in the ranking context.

### Problem: Thin trafilatura vs. rich trafilatura
HuggingFace trafilatura = 1600 chars (navigation only — useless).
Amazon trafilatura = 7600 chars (product names, prices, specs — valuable).

**Fix:** If `len(trafilatura) >= 3000`, use it as the listing snapshot. Otherwise, skip it and rely on the ranking context from detail page card links.

---

## Project Structure

```
Assignment9/
├── S9SharedCode/
│   └── code/
│       ├── flow.py              # Orchestrator / Executor
│       ├── skills.py            # Skill registry, render_prompt, resolve_inputs
│       ├── recovery.py          # Failure classification & critic-fail handling
│       ├── schemas.py           # AgentResult, BrowserOutput, NodeSpec
│       ├── graph.py             # NetworkX DAG wrapper
│       └── browser/
│           ├── skill.py         # Browser cascade + detail page following
│           ├── driver.py        # A11yDriver, SetOfMarksDriver
│           ├── client.py        # V9 LLM gateway client
│           └── dom.py           # DOM helpers
├── llm_gatewayV9/               # LLM gateway service
├── logs.txt                     # Sample run logs
└── study_notes.tex              # Architecture study notes (PDF)
```

---

## Built With

- **Python 3.12+** with `uv` for environment management
- **Playwright** — browser automation
- **Trafilatura** — HTML content extraction
- **NetworkX** — DAG graph management
- **AsyncIO** — parallel node execution
- **Anthropic Claude** — LLM backbone for all skill nodes
