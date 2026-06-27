# CUA Skill Function Graphs

Traces the full call chain for each Computer Use Agent skill — from the moment
`uv run python flow.py "..."` is run to when the final answer is printed.

---

## How to read these graphs

Every query goes through the same outer loop in `flow.py`:

```
main()
  └── Executor.run(query)
        ├── Planner node   — LLM decides which skill to use and what metadata to pass
        ├── CUA skill node — the skill executes (controls the app)
        └── Formatter node — LLM renders the final human-readable answer
```

The CUA skill is what differs between the three examples below.

---

## 1. cua_hotkey — Calculator example

```
uv run python flow.py "open calculator app and do the calculation 48 multiplied by 125"
```

```
main()
└── asyncio.run(Executor().run(query))
    ├── ensure_gateway()
    ├── SkillRegistry()                        # loads agent_config.yaml
    ├── memory_svc.read(query)                 # FAISS lookup
    ├── memory_svc.remember(query)
    ├── Graph.add_node("planner", ["USER_QUERY"])
    │
    │   ── LOOP iteration 1: planner ──
    ├── Graph.ready_nodes()
    ├── Executor._run_one(planner_node)
    │   ├── run_skill()
    │   │   ├── resolve_inputs()               # materialise USER_QUERY
    │   │   ├── render_prompt()                # inject memory hits + inputs
    │   │   └── LLM.chat()                     # gateway → Gemini
    │   │       └── parse_skill_json()         # extract successors (NodeSpec list)
    │   └── Graph.extend_from()                # splices cua_hotkey node into DAG
    │
    │   ── LOOP iteration 2: cua_hotkey ──
    ├── Graph.ready_nodes()
    ├── Executor._run_one(hotkey_node)
    │   └── run_skill()
    │       ├── resolve_inputs()
    │       ├── render_prompt()
    │       └── HotkeySkill.run(NodeSpec)
    │           ├── _osascript('tell application "Calculator" to activate')
    │           ├── asyncio.sleep(1.0)
    │           ├── [step 1] _osascript(keystroke "48*125=")
    │           ├── [step 2] _osascript(delay 1s)
    │           └── _osascript(read AX path)   # reads "6,000" from display
    │
    │   ── LOOP iteration 3: formatter ──
    └── Executor._run_one(formatter_node)
        └── run_skill()
            ├── resolve_inputs()               # includes hotkey output {"result": "6,000"}
            ├── render_prompt()
            └── LLM.chat()                     # gateway → Gemini → final answer text
```

**LLM calls inside the skill: 0**
The Planner decides what keystrokes to send upfront and puts them in `metadata.steps`.
The skill just executes them via `osascript` — no thinking at runtime.

---

## 2. cua_electron — VS Code example

```
uv run python flow.py "Open VS Code IDE, create a new file called hello.py, type a Python hello world, and save it"
```

```
main()
└── asyncio.run(Executor().run(query))
    ├── ensure_gateway(), SkillRegistry()
    ├── memory_svc.read(), memory_svc.remember()
    ├── Graph.add_node("planner", ["USER_QUERY"])
    │
    │   ── LOOP iteration 1: planner ──
    ├── Executor._run_one(planner_node)
    │   ├── run_skill() → LLM.chat() → parse_skill_json()
    │   └── Graph.extend_from()                # adds cua_electron node
    │
    │   ── LOOP iteration 2: cua_electron ──
    ├── Executor._run_one(electron_node)
    │   └── run_skill()
    │       ├── resolve_inputs(), render_prompt()
    │       └── ElectronSkill.run(NodeSpec)
    │           │
    │           ├── _launch(app_path, port=9222, workspace)
    │           │   ├── _resolve_binary(app_path)   # reads Info.plist → binary path
    │           │   ├── tempfile.mkdtemp()           # isolated user-data-dir
    │           │   ├── write session.code-workspace JSON
    │           │   ├── subprocess.Popen([Code, --remote-debugging-port=9222, ...])
    │           │   └── asyncio.sleep(4.0)           # wait for VS Code to start
    │           │
    │           ├── _wait_for_cdp(port=9222)
    │           │   └── httpx.AsyncClient.get("http://localhost:9222/json")  # poll until ready
    │           │
    │           ├── async_playwright()
    │           │   ├── p.chromium.connect_over_cdp("http://localhost:9222")
    │           │   └── _find_workbench_page(browser)  # skips devtools/extension pages
    │           │
    │           ├── page.keyboard.press("Escape") ×3  # dismiss first-run dialogs
    │           ├── page.keyboard.press("Meta+w")      # close Welcome tab
    │           │
    │           ├── V9Client(gateway_url, agent="cua_electron")
    │           ├── _extract_file_plan(goal, client)           ← only LLM call in skill
    │           │   └── client.chat(prompt, schema=file_plan_schema)
    │           │       └── gateway → Gemini → {"filename": "hello.py", "content": "..."}
    │           │
    │           └── _create_and_save(page, "hello.py", 'print("Hello, World!")')
    │               ├── page.keyboard.press("Meta+Shift+P")    # open command palette
    │               ├── page.keyboard.type("File: New File")
    │               ├── page.keyboard.press("ArrowDown")       # select correct dropdown item
    │               ├── page.keyboard.press("Enter")           # open VS Code filename prompt
    │               ├── page.keyboard.type("hello.py")
    │               ├── page.keyboard.press("Enter")           # create file in workspace
    │               ├── page.keyboard.type('print("Hello, World!")')
    │               ├── page.keyboard.press("Meta+s")          # save
    │               ├── page.keyboard.press("Control+`")       # open terminal
    │               ├── page.keyboard.type("uv run python hello.py")
    │               └── page.keyboard.press("Enter")           # run file
    │           [finally] proc.terminate()
    │
    │   ── LOOP iteration 3: formatter ──
    └── Executor._run_one(formatter_node)
        └── run_skill() → LLM.chat() → final answer
```

**LLM calls inside the skill: 1**
One targeted call to `_extract_file_plan` extracts `filename` and `content` from the
goal string. Everything after that is a fully scripted Playwright sequence — no LLM
is involved in the actual VS Code interaction.

---

## 3. cua_game — 2048 example

```
uv run python flow.py "play 2048 at https://play2048.co/ for 10 moves and report the highest tile reached"
```

```
main()
└── asyncio.run(Executor().run(query))
    ├── ensure_gateway(), SkillRegistry()
    ├── memory_svc.read(), memory_svc.remember()
    ├── Graph.add_node("planner", ["USER_QUERY"])
    │
    │   ── LOOP iteration 1: planner ──
    ├── Executor._run_one(planner_node)
    │   ├── run_skill() → LLM.chat() → parse_skill_json()
    │   └── Graph.extend_from()                # adds cua_game node
    │
    │   ── LOOP iteration 2: cua_game ──
    ├── Executor._run_one(game_node)
    │   └── run_skill()
    │       ├── resolve_inputs(), render_prompt()
    │       └── BrowserGameSkill.run(NodeSpec)
    │           │
    │           ├── async_playwright()
    │           │   ├── p.chromium.launch(headless=False)
    │           │   ├── browser.new_context(viewport=600×720)
    │           │   └── ctx.new_page()
    │           │
    │           ├── page.goto("https://play2048.co/")
    │           ├── asyncio.sleep(2.5)
    │           │
    │           ├── _dismiss_popups(page)
    │           │   ├── page.keyboard.press("Escape")
    │           │   ├── for each CMP selector:
    │           │   │   └── page.locator(sel).is_visible() → .click() if found
    │           │   └── page.keyboard.press("Escape")
    │           │
    │           ├── page.mouse.click(300, 350)             # give game keyboard focus
    │           │
    │           └── for turn in 1..10:                     ← vision loop
    │               ├── page.screenshot()
    │               ├── _png_to_data_url(png_bytes)        # base64 encode
    │               ├── V9Client.vision(data_url, prompt, schema=game_action_schema)
    │               │   └── POST /v1/vision → gateway → Gemini
    │               │       └── {"key": "ArrowRight", "game_state": "...", "done": false}
    │               ├── if key not in allowed_keys:
    │               │   └── random.choice(allowed_keys)   # fallback
    │               └── page.keyboard.press(key)
    │
    │           ├── page.screenshot()                      # final board
    │           ├── V9Client.vision(final_url, "describe final state")
    │           └── [finally] browser.close()
    │
    │   ── LOOP iteration 3: formatter ──
    └── Executor._run_one(formatter_node)
        └── run_skill() → LLM.chat() → final answer
```

**LLM calls inside the skill: 11** (10 move decisions + 1 final summary)
Every turn the skill takes a screenshot, sends it to the vision model, and presses
whichever arrow key the model returns. The LLM is the decision engine for every move.

---

## Comparison

| | cua_hotkey | cua_electron | cua_game |
|---|---|---|---|
| **LLM calls inside skill** | 0 | 1 | 11 (10 moves + summary) |
| **How app is controlled** | `osascript` subprocess | Playwright over CDP | Playwright keyboard in browser |
| **Interaction style** | Fully scripted from Planner metadata | Scripted after LLM extracts filename/content | LLM decides each move from screenshot |
| **Where thinking happens** | Planner only (before skill runs) | Planner + one targeted pre-flight LLM call | Inside the skill loop, every turn |
| **CDP involved** | No — native macOS app | Yes — VS Code is an Electron app | Yes — it is a browser tab |
| **Input to LLM inside skill** | Nothing | Goal string | Screenshot (image) |
| **Output from LLM inside skill** | Nothing | JSON: `{filename, content}` | JSON: `{key, game_state, done}` |

---

## Key engineering details

### cua_hotkey — why zero LLM calls at runtime
The Planner emits the complete `steps` list in `metadata` when it creates the node.
`HotkeySkill.run()` just iterates over those steps and dispatches each one via
`osascript`. The AX (Accessibility) path to read the result back is also supplied
by the Planner upfront.

### cua_electron — why ArrowDown before Enter matters
VS Code's command palette has two types of entries: commands and file paths.
Pressing Enter immediately executes the top match which could be a recently opened
file path rather than the `File: New File` command. `ArrowDown` moves the selection
to the command entry, ensuring VS Code shows its own filename prompt. This keeps
everything inside the Electron window — if a native macOS Save dialog appeared,
Playwright cannot reach it over CDP.

### cua_game — why the JSON schema has an enum on the key field
Early versions had `key` as an optional string. The vision model returned an empty
string every turn and no moves were made. Adding `"enum": ["ArrowUp", "ArrowDown",
"ArrowLeft", "ArrowRight"]` to the schema forces the model to always return a valid
arrow key. A `random.choice` fallback in Python handles the edge case where schema
validation still fails.
