# Skills, Tools, and LLM Dispatch — Interview Study Notes

---

## 1. What is a Skill?

A **skill** is one node type in the execution graph. Each skill has:
- A **prompt template** (what role it plays)
- A **list of tools it is allowed to use** (can be empty)
- Configuration like temperature, max_tokens

Skills are declared in `agent_config.yaml`:

```yaml
researcher:
  prompt: prompts/researcher.md
  tools_allowed: [web_search, fetch_url]
  temperature: 0.7
  max_tokens: 2500

planner:
  prompt: prompts/planner.md
  tools_allowed: []       # no tools — text only
  temperature: 0.4

distiller:
  prompt: prompts/distiller.md
  critic: true            # auto-inserts a Critic node after it
  tools_allowed: []
  temperature: 0.1

browser:
  prompt: prompts/browser.md
  tools_allowed: []       # explained in detail below
  temperature: 0.0
```

---

## 2. How Tools Are Wired to a Skill

There are 3 steps:

### Step 1 — Declare in `agent_config.yaml`
Each skill lists tool names it is allowed to use:
```yaml
researcher:
  tools_allowed: [web_search, fetch_url]

retriever:
  tools_allowed: [search_knowledge]
```

### Step 2 — Tool schemas defined in `skills.py`
A `_TOOL_CATALOG` dictionary holds the full description of every tool — what it does, what inputs it expects:

```python
_TOOL_CATALOG = {
    "web_search": {
        "name": "web_search",
        "description": "Search the web. Hard-capped at 5 results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
    "fetch_url": { ... },
    "search_knowledge": { ... },
}
```

### Step 3 — At runtime, schemas sent to LLM as a separate field
When a skill node runs, `tool_payload()` looks up the tool names from `tools_allowed`, fetches their schemas, and passes them to the LLM via the `tools=` parameter:

```python
tools = tool_payload(skill.tools_allowed)  # fetches schemas for allowed tools
reply = await run_with_tools(
    prompt=rendered,
    tools_payload=tools,   # passed separately — NOT injected into prompt text
    agent=skill.name,
    ...
)
```

---

## 3. Tools Are NOT Inserted Into the System Prompt

This is a common misconception. Tools are passed as a **separate field** in the API call — not mixed into the prompt text.

### What the API call actually looks like:

```python
LLM().chat(
    messages=[{"role": "user", "content": "find laptops under 80000"}],  # prompt here
    tools=[                                                                 # tools here — separate
        {
            "name": "web_search",
            "description": "Search the web...",
            "input_schema": { ... }
        }
    ],
    tool_choice="auto",
)
```

Think of it like a form with two sections:

```
┌─────────────────────────────────────┐
│  messages (text)                    │  ← skill prompt + conversation history
│  - role: system → skill prompt      │
│  - role: user   → "find laptops"    │
├─────────────────────────────────────┤
│  tools (structured)                 │  ← tool schemas — separate slot
│  - web_search schema                │
│  - fetch_url schema                 │
└─────────────────────────────────────┘
```

The LLM reads BOTH sections in the same API call. You don't need to mention tools in the prompt text — the model is designed to look for the `tools` field automatically.

---

## 4. How Does the LLM Know Tools Exist If Not in the Prompt?

The LLM provider (Anthropic/OpenAI) built this natively into the model **during training**. The model was specifically trained to:

- Look for a `tools` block in the incoming request
- Understand the schemas in that block as "things I can actually invoke"
- Return a structured `tool_call` response when it wants to use one
- Return normal text when it does not need a tool

It is not magic — the model was **taught during training** that if a `tools` block is present, those are callable functions. It is part of how the model works, not something you need to explain in the prompt.

---

## 5. Why Separate Tools from the System Prompt? — 4 Real Advantages

### Advantage 1 — Structured tool calls, not free text to parse

**If tools were in the system prompt (old way):**
The LLM might say:
```
I will call web_search with query="laptops under 80000"
```
You now have to *parse* that sentence to extract the tool name and arguments. Fragile — the model might format it differently each time.

**With `tools=` (native):**
The LLM returns a clean JSON object:
```json
{"name": "web_search", "arguments": {"query": "laptops under 80000"}}
```
No parsing needed. Ready to execute directly.

### Advantage 2 — The LLM makes better decisions

When tools are in the system prompt as text, the LLM sees them as "descriptions" — it has no real understanding that these are callable functions.

With `tools=`, the model is specifically trained to understand "these are things I can actually invoke." It makes better decisions about *when* to call a tool vs. *when* to answer from memory.

### Advantage 3 — Token efficiency and caching

If tool schemas are in the system prompt, they eat into your context window on every call and are processed as regular text.

With `tools=`, providers like Anthropic can cache them separately. They don't count the same way against your prompt tokens on repeated calls — especially useful when the same tools are used across many skill invocations in a session.

### Advantage 4 — Automatic validation

The LLM is constrained to only call tools that exist in the `tools=` list and must pass the correct argument types defined in the schema.

If tools were in the system prompt as text, nothing stops the model from hallucinating a tool name that doesn't exist or passing wrong argument types.

---

## 6. The Multi-Turn Tool Loop (mcp_runner.py)

When a skill has `tools_allowed`, execution goes through `run_with_tools()` instead of a simple one-shot LLM call. This runs a loop:

```
1. Send messages + tool schemas to LLM
2. LLM replies with a tool_call
   e.g. {"name": "web_search", "arguments": {"query": "..."}}
3. Python code actually executes the tool via MCP
4. Tool result appended to messages as role="tool"
5. Send messages to LLM again
6. Repeat until LLM returns plain text (no more tool calls)
7. Return final text answer
```

In code (`mcp_runner.py`):

```python
messages = [{"role": "user", "content": prompt}]

for _ in range(MAX_TOOL_HOPS):          # hard cap = 6 hops
    reply = await _chat(messages=messages, tools=tools_payload, ...)

    if not reply.get("tool_calls"):
        return reply                     # LLM finished — return answer

    # LLM called a tool — execute it and feed result back
    messages.append({"role": "assistant", "tool_calls": reply["tool_calls"]})
    for tc in reply["tool_calls"]:
        result = await _dispatch_tool(mcp, tc["name"], tc["arguments"])
        messages.append({"role": "tool", "content": result})
    # loop again — LLM now has the tool result and can continue
```

**Key point:** The LLM is in control — it decides when to call a tool and when it has enough information to answer. Python code just executes whatever the LLM asks for and feeds the result back.

---

## 7. The Browser Skill — A Special Case

Looking at `agent_config.yaml`:
```yaml
browser:
  tools_allowed: []   # no tools!
```

But the browser skill clearly uses an LLM internally to decide what to click on a page. So what is going on?

### The browser skill bypasses normal dispatch entirely

Every other skill goes through this path in `skills.py`:
```
run_skill() → render_prompt() → LLM().chat() → gateway
```

But when `skill.name == "browser"`, it takes a completely different path:

```python
if skill.name == "browser":
    # bypasses render_prompt and gateway-chat dispatch entirely
    from browser.skill import BrowserSkill
    sk = BrowserSkill(...)
    result = await sk.run(node_spec)   # owns everything itself
    return result
```

The browser skill is **self-contained**. It has its own internal LLM client (`V9Client`) that it creates and manages itself. It calls the gateway directly via HTTP — completely independent of the normal skill dispatch.

### Why no `tools_allowed` then?

`tools_allowed` in agent_config.yaml means:
> "Give this LLM the ability to call external tools ON ITS OWN INITIATIVE during its response"

But in the browser skill, the LLM is **never given autonomous tool access**. The Python code (the cascade) is in control at all times:

```
Python code: "here is the page DOM, here is the goal — what to click?"
LLM:         "click the Text Generation filter"
Python code: clicks it, page updates
Python code: "here is the new page — what to click next?"
LLM:         "click Sort by Likes"
Python code: clicks it
```

The LLM just answers "what next?" each time Python asks. It does not decide on its own to call `web_search` or anything else. It only sees the current page state and returns a click decision.

### The cascade itself needs no tools

```
Layer 1  → trafilatura HTML extract     (no LLM at all)
Layer 2a → deterministic CSS selectors  (no LLM at all)
Layer 2b → A11y driver                  (LLM decides what to click)
Layer 3  → Vision/SoM driver            (LLM decides what to click)
```

The LLM in layers 2b and 3 is used as a **decision maker**, not as an **autonomous agent with tool access**. This is why no `tools_allowed` is declared — the browser skill carries everything it needs internally.

---

## 8. Summary — Skills vs Tools vs LLM Role

| Skill | `tools_allowed` | LLM role | Who is in control |
|---|---|---|---|
| `planner` | none | Plans the DAG | LLM reasons freely |
| `researcher` | web_search, fetch_url | Researches and decides when to search | LLM in control — calls tools on its own |
| `retriever` | search_knowledge | Queries knowledge base | LLM in control — calls tools on its own |
| `distiller` | none | Extracts structured fields from given text | LLM reasons freely |
| `critic` | none | Passes or fails the upstream output | LLM reasons freely |
| `formatter` | none | Renders final answer | LLM reasons freely |
| `browser` | none (declared) | Decides what to click on each page state | Python code in control — LLM just answers |

---

## 9. Key Interview Talking Points

**Q: How are tools associated with skills?**
Declared in `agent_config.yaml` under `tools_allowed`. At runtime, `tool_payload()` fetches their schemas from `_TOOL_CATALOG` and passes them as `tools=` in the API call.

**Q: Why not put tool descriptions in the system prompt?**
Native `tools=` gives structured JSON tool calls (no parsing), better LLM decision-making, token caching, and automatic argument validation. System prompt injection is fragile and requires custom parsing.

**Q: How does the LLM know tools exist if not in the prompt?**
LLM providers train the model to look for a `tools` block in the API request. It is a first-class feature of the model — not something that needs to be explained in text.

**Q: Why does the browser skill have no `tools_allowed`?**
Because `tools_allowed` gives the LLM autonomous tool-calling ability. The browser skill's LLM is not autonomous — it only answers "what to click" when Python code asks. The cascade logic (which layer to try, when to escalate) is entirely in Python code, not delegated to the LLM.

**Q: What is the difference between researcher and browser in terms of LLM usage?**
Researcher's LLM is in control — it decides when to call `web_search` and reads the result itself. Browser's LLM is a passenger — Python code drives the cascade, opens the browser, and only asks the LLM "what to click next?" The LLM has no steering wheel in the browser skill.
