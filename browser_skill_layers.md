# Browser Skill — 4 Layers Deep Dive

The browser skill is a cascade of 4 layers. Each layer tries to extract or
interact with a web page in a different way. If one layer fails or is
insufficient, the next layer is triggered automatically. The skill stops at
the first layer that succeeds.

---

## Why 4 Layers?

Different websites require different levels of effort to read:

- A simple news article → static HTML, no interaction needed → Layer 1 is enough
- A React app like HuggingFace → page is built by JavaScript → need a real browser
- A filtered/sorted listing → need to click buttons → need an LLM to decide what to click
- A canvas-based widget → not in the HTML accessibility tree at all → need to literally see the screen

One layer cannot handle all of these. So the skill tries the cheapest approach first and escalates only when needed.

---

## Layer 1 — Pure HTML Extract (No LLM)

### What it does

Downloads the raw HTML of the page using `httpx` (a plain HTTP request, no
browser opened at all) and runs it through `trafilatura` — a library that
strips navigation, ads, and boilerplate and returns only the main readable
text content.

**This layer uses zero LLM calls. It is pure Python.**

### How it is triggered

Always runs first, automatically, for every browser node.

### How it decides to escalate

Two conditions are checked by `_is_useful_extract()`:

**Condition 1 — Content too thin:**
```python
if len(content) < 200:
    return False   # escalate
```

**Condition 2 — Goal requires interaction:**
```python
interactive_verbs = ("click", "fill", "select", "type", "filter", "sort", ...)
if any(v in goal.lower() for v in interactive_verbs):
    return False   # escalate — you cannot "sort" by reading HTML
```

### Examples

```
Goal: "extract main content from this article"
  → Layer 1 fetches HTML → trafilatura gets 5000 chars of clean article text
  → is_useful = True → DONE ✓  (no escalation, no LLM used at all)
```

```
Goal: "Filter by Text Generation, Sort by Most Likes"
  → Layer 1 fetches HTML → trafilatura gets 1605 chars
  → goal contains "Filter" and "Sort" → is_useful = False → ESCALATE
  → (even if content was large, interaction goals always escalate)
```

```
Goal: "extract model names from HuggingFace"
  → Layer 1 fetches HTML → trafilatura gets only 1605 chars (React SPA)
  → len(content) < 200? No, but content is navigation boilerplate
  → is_useful = False → ESCALATE
```

### What happens in the logs

```
[browser] layer1: fetching HTML via httpx ...
[browser] layer1: fetch ok — 331123 chars, final_url=https://huggingface.co/models
[browser] layer1: extract → 1416 chars, is_useful=False
[browser] layer1: extract insufficient — escalating
```

---

## Layer 2a — Deterministic Selectors (No LLM)

### What it does

The Planner can optionally include exact CSS selectors in the node's metadata.
This layer executes those instructions mechanically using Playwright. No LLM
is involved — no guessing, no reasoning. Just: find this element, click it.

**This layer uses zero LLM calls. It is pure Playwright automation.**

### How it is triggered

Only runs if `metadata.selectors` is present in the node spec. If the Planner
did not provide selectors, this layer is **skipped entirely**.

### Example

```yaml
# Planner emits a node like this:
{
  "skill": "browser",
  "metadata": {
    "url": "https://example.com/products",
    "goal": "filter laptops",
    "selectors": [
      {"action": "click", "selector": "#filter-button"},
      {"action": "click", "selector": ".category-laptop"}
    ]
  }
}
```

Layer 2a finds `#filter-button` in the DOM and clicks it, then clicks
`.category-laptop`. No LLM needed.

### How it decides to escalate

If the selector does not find the element, or the action fails → escalate to
Layer 2b. Also, in practice the Planner rarely provides selectors (it would
need to know the site's DOM structure in advance), so this layer is almost
always skipped.

### What happens in the logs

```
[browser] layer2a: no selectors in metadata — skipping
```

or if selectors were provided:

```
[browser] layer2a: trying 2 deterministic selector(s) ...
[browser] layer2a: deterministic returned None — escalating to a11y
```

---

## Layer 2b — A11y Driver (LLM driven, text only)

### What it does

Opens a **real browser** using Playwright, loads the page, and reads the
**accessibility tree** — a structured list of every interactive element on
the page (buttons, inputs, dropdowns, links) with their names and roles.
This is the same data that screen readers use.

It does **NOT** take screenshots. It only sends text to the LLM.

Then it runs a loop:
1. Read the current accessibility tree
2. Send it to the LLM → ask "what should I click next?"
3. LLM responds with an action
4. Python executes the action (click, type, scroll, etc.)
5. Page updates → go back to step 1
6. Repeat until LLM says `done`

### What the LLM sees each turn

```
GOAL: Filter by Task: Text Generation, Sort by: Most Likes

PAGE URL: https://huggingface.co/models
VIEWPORT: 1366x900
INTERACTIVE ELEMENTS (47):
  [1]<button role="button">Tasks</button>
  [2]<button role="button">Languages</button>
  [3]<input role="searchbox">Search models</input>
  [4]<button role="button">Sort: Trending ▾</button>
  [5]<link>deepseek-ai/DeepSeek-R1</link>
  [6]<link>meta-llama/Meta-Llama-3-8B</link>
  ...

RECENT ACTIONS:
  (no actions yet)

What is the next set of actions?
```

### What the LLM responds with

```json
{
  "thinking": "I need to click Tasks to open the task filter dropdown",
  "actions": [
    {"type": "click", "mark": 1}
  ]
}
```

Python clicks element `[1]`. Page updates. New accessibility tree is read.
Sent to LLM again.

**Turn 2:**
```
INTERACTIVE ELEMENTS:
  [1]<button>Text Generation</button>
  [2]<button>Text Classification</button>
  [3]<button>Token Classification</button>
  ...

RECENT ACTIONS:
  turn 1: click(1) → ok
```

LLM responds:
```json
{
  "thinking": "Text Generation filter is now visible, clicking it",
  "actions": [
    {"type": "click", "mark": 1}
  ]
}
```

This continues until the LLM says:
```json
{
  "thinking": "Page is now filtered by Text Generation and sorted by likes",
  "actions": [
    {"type": "done", "success": true, "note": "filtering complete"}
  ]
}
```

### The action vocabulary

The LLM can emit these actions:

| Action | What it does |
|---|---|
| `click(mark)` | Click the element with that number |
| `type(mark, value)` | Type text into an input field |
| `key(value)` | Press a keyboard key like Enter, Tab, Escape |
| `scroll(direction, amount)` | Scroll the page up/down/left/right |
| `drag(from_x, from_y, to_x, to_y)` | Mouse drag (for sliders, canvas) |
| `wait(seconds)` | Pause for the page to load/settle |
| `done(success, note)` | Signal that the goal is complete or impossible |

### Hard limits

- Max **12 turns** per run (configurable via `max_steps_a11y`)
- Max **3 consecutive errors** before giving up
- Max **2 actions per turn** (LLM is told to emit one action most of the time)

### How it decides to escalate to Layer 3

If `driver.run()` returns `success=False` for any reason:
- LLM said `done(success=false)` — "I cannot complete this goal"
- Hit the 12-turn cap without finishing
- Got 3 consecutive Playwright errors

→ Escalate to Layer 3

### What happens in the logs

```
[browser] layer2b: running A11yDriver ...
[browser._drive] A11yDriver: navigating to https://huggingface.co/models ...
[browser._drive] A11yDriver: page loaded — https://huggingface.co/models
[browser._drive] A11yDriver: rendered HTML = 331113 chars
[browser._drive] A11yDriver: post-JS gateway block check → None
[browser._drive] A11yDriver: driver.run() done — success=True  note=''
[browser] layer2b: ✓ a11y succeeded — returning a11y
```

---

## Layer 3 — Vision / Set-of-Marks Driver (LLM driven, with screenshots)

### What it does

Identical loop to Layer 2b BUT in addition to the text accessibility tree,
it also takes a **screenshot** of the page and draws **numbered dashed boxes**
over every interactive element. This annotated screenshot is sent to the LLM
alongside the text legend.

The LLM now literally **sees the page** as a human would.

### Why this exists

Some pages have elements that are invisible to the accessibility tree:
- Canvas-based UI components
- Custom-rendered dropdowns
- SVG-based interactive charts
- Visual widgets with no proper HTML role

Layer 2b would be completely blind to these. Layer 3 can see them because it
is looking at the actual rendered screenshot.

### What the LLM sees each turn

```
[annotated screenshot attached — page with numbered red boxes over every
 interactive element visible on screen]

GOAL: Filter by Text Generation, Sort by Most Likes

VIEWPORT: 1366x900 (CSS px, dpr=2)
INTERACTIVE ELEMENTS (47):
  [1]<button>Tasks</button>
  [4]<button>Sort: Trending ▾</button>
  ...

RECENT ACTIONS:
  turn 1: click(4) → ok

What is the next set of actions?
```

The LLM can see box `[4]` on the screenshot AND read the text legend — it has
full context.

### Key difference from Layer 2b

| | Layer 2b (A11y) | Layer 3 (Vision) |
|---|---|---|
| What LLM sees | Text list only | Screenshot + text list |
| LLM call endpoint | `/v1/chat` | `/v1/vision` |
| Model required | Any text model | Vision-capable model |
| Cost | Cheaper | More expensive |
| Works on | Standard HTML pages | Anything visible on screen |
| Blind to | Non-semantic elements | Nothing — sees the screen |

### How it decides to escalate

If Layer 3 also returns `success=False` → there is nowhere left to go.
The skill returns `blocked`:

```python
return self._pack(url, goal, "blocked", turns=0,
    content="blocked: all layers exhausted; last: step cap reached (12)")
```

This tells the orchestrator "I tried everything — this page cannot be
accessed." The orchestrator treats this as a terminal result (not a retry).

### What happens in the logs

```
[browser] layer3: running SetOfMarksDriver ...
[browser] layer3: vision done — success=True  gateway_blocked=False
[browser] layer3: ✓ vision succeeded — returning vision
```

or if everything fails:

```
[browser] layer3: vision failed — all layers exhausted
[browser] ✗ returning blocked — last_err: step cap reached (12)
```

---

## The Full Cascade Flow

```
URL + Goal given to browser skill
          │
          ▼
┌─────────────────────────────┐
│  Layer 1: httpx + trafilatura │  No LLM. Pure HTTP + text extraction.
└─────────────────────────────┘
          │
          ├── content useful AND goal is read-only? ──────────────► DONE ✓
          │
          └── thin content OR interactive goal?
                    │
                    ▼
┌──────────────────────────────────────────┐
│  Layer 2a: Deterministic CSS selectors   │  No LLM. Playwright only.
│  (only if Planner gave selectors)        │
└──────────────────────────────────────────┘
          │
          ├── selectors worked? ────────────────────────────────── DONE ✓
          │
          └── no selectors / failed
                    │
                    ▼
┌──────────────────────────────────────────┐
│  Layer 2b: A11y Driver                   │  LLM driven. Text only.
│  - open real browser (Playwright)         │
│  - read accessibility tree each turn     │
│  - LLM: "what to click?" → click        │
│  - repeat up to 12 turns                 │
└──────────────────────────────────────────┘
          │
          ├── LLM says done(success=true)? ────────────────────── DONE ✓
          │
          └── max steps OR consecutive failures
                    │
                    ▼
┌──────────────────────────────────────────┐
│  Layer 3: Vision / Set-of-Marks Driver   │  LLM driven. Screenshot + text.
│  - screenshot + draw numbered boxes      │
│  - LLM sees the page visually            │
│  - LLM: "what to click?" → click        │
│  - repeat up to 12 turns                 │
└──────────────────────────────────────────┘
          │
          ├── LLM says done(success=true)? ────────────────────── DONE ✓
          │
          └── all failed ──────────────────────────────────────── BLOCKED ✗
                                                  (orchestrator gets
                                                   error_code=blocked)
```

---

## How the LLM Loop Works Inside Layers 2b and 3

This is the core loop inside `BaseDriver.run()`:

```
for turn in 1..max_steps:
    1. snap = read accessibility tree from current page
    2. parsed = LLM._decide(snap, turn)
       → sends prompt (+ screenshot for Layer 3) to LLM
       → LLM returns JSON with thinking + actions list
    3. for each action in parsed.actions:
         if action is "done":
             return DriverResult(success, note)
         else:
             Playwright executes the action (click, type, etc.)
    4. wait 0.5s for page to settle
    5. next turn
```

**Key point:** The LLM does NOT call tools here. It is not in an autonomous tool
loop. Python code drives the turns — it asks the LLM "what next?" and then
executes whatever the LLM says. The LLM is a decision maker, not an agent
with autonomy.

---

## Why Layer 2b Before Layer 3?

**Cost.** Layer 2b calls `/v1/chat` (text only) which is much cheaper than
Layer 3 which calls `/v1/vision` (requires a vision-capable model, more tokens
due to image encoding).

Most interactive pages (HuggingFace, Amazon, GitHub) work fine with Layer 2b
because their buttons and dropdowns are standard HTML and appear in the
accessibility tree. Layer 3 is reserved for pages with unusual visual-only
components.

---

## Real Example — HuggingFace Query

Query: `"Compare top 3 HuggingFace text-generation models sorted by likes"`

```
Layer 1:
  → fetches https://huggingface.co/models
  → trafilatura gets 1605 chars (React SPA — mostly navigation)
  → goal contains "sorted" → is_useful=False → ESCALATE

Layer 2a:
  → no selectors in metadata → SKIP

Layer 2b (A11y):
  → opens browser, loads https://huggingface.co/models
  → Turn 1: reads accessibility tree, sends to LLM
  → LLM: click Tasks button
  → Turn 2: task list appears, LLM: click Text Generation
  → Turn 3: filter applied, LLM: click Sort dropdown
  → Turn 4: sort options appear, LLM: click Most Likes
  → Turn 5: page sorted by likes, LLM: done(success=true)
  → DONE ✓

Layer 3: never reached
```

---

## What is Playwright and Why is it Here?

Playwright is a library made by **Microsoft** that lets Python code control a real web browser
(Chrome, Firefox, Safari) automatically — exactly like a human sitting at a computer clicking
through a website.

### What Playwright can do

```python
browser = await p.chromium.launch()       # open a real Chrome browser
page    = await browser.new_page()

await page.goto("https://amazon.in")      # navigate to a URL
await page.click("#filter-button")        # click a button
await page.keyboard.type("laptops")       # type in a search box
await page.screenshot()                   # take a screenshot
await page.content()                      # read the full rendered HTML
await page.inner_text("body")             # read only the visible text
```

All of this happens in a **real browser** — JavaScript runs, React renders, animations play —
exactly as if a human was using it.

### Why Playwright is needed here

Without Playwright (Layer 1 only):
```
httpx fetches raw HTML → gets 1605 chars of navigation text
→ React hasn't run yet → model cards are missing → distiller hallucinates
```

With Playwright (Layer 2b / 3):
```
Playwright opens Chrome → page fully loads → React runs → model cards render
→ LLM reads accessibility tree → clicks "Text Generation" filter
→ clicks "Sort by Likes" → page shows sorted results
→ browser extracts 8000 chars of real content
```

### Simple analogy

| Tool | Analogy |
|---|---|
| `httpx` (Layer 1) | Taking a photo of a shop's front door from outside |
| `Playwright` (Layer 2b / 3) | Actually walking into the shop, opening drawers, pressing buttons |

Some websites (especially React/SPA apps like HuggingFace, Amazon) only show their real
content **after** JavaScript runs. `httpx` sees the empty shell. Playwright sees the fully
loaded page — because it IS a real browser.

### Where Playwright is used in the cascade

| Layer | Uses Playwright? | What it does with it |
|---|---|---|
| Layer 1 | No | Pure `httpx` HTTP request — no browser |
| Layer 2a | Yes | Opens browser, finds a CSS selector, clicks it |
| Layer 2b | Yes | Opens browser, reads accessibility tree, executes LLM-chosen clicks |
| Layer 3 | Yes | Same as 2b, PLUS takes screenshots with numbered boxes drawn on them |

---

## Key Interview Talking Points

**Q: Why not just use a real browser for everything (skip Layer 1)?**
Because opening Playwright takes ~2-3 seconds and costs more. For a simple
article or static page, Layer 1 answers in milliseconds with zero LLM cost.
Escalate only when necessary.

**Q: What is the accessibility tree?**
A structured representation of every interactive element on a page — same data
screen readers use. Each element has an id, tag, role, and name. Layer 2b
sends this as text to the LLM instead of a screenshot — cheaper but still lets
the LLM understand what is on the page.

**Q: What is Set-of-Marks?**
A technique where numbered boxes are drawn over interactive elements in a
screenshot. The LLM sees the visual page AND a text legend mapping each number
to an element. When the LLM says `click(mark=4)`, it means "click the element
in box number 4."

**Q: How does the LLM know when it is done?**
It emits a `done` action with `success=true` or `success=false`. Python code
checks for this action in each turn's response and exits the loop.

**Q: What stops the LLM from looping forever?**
Hard cap of 12 turns (`max_steps`). If the goal is not complete by turn 12,
the driver returns `success=False` and the skill escalates to the next layer.
Also, 3 consecutive Playwright errors trigger an early exit.

**Q: What happens if all 4 layers fail?**
The skill returns `blocked` with `success=True`. Yes — `success=True` even
on failure. This is intentional: it stops the orchestrator from treating the
browser node as a recoverable failure and triggering an infinite retry loop.
`blocked` means "dead end, move on."
