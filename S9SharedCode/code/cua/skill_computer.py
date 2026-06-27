"""CUA ComputerSkill — unified layered computer use agent.

Tries three layers in order, exactly like the browser skill cascade:

  Layer 1  →  Hotkey / osascript     (native macOS, zero LLM calls)
  Layer 2  →  Electron / CDP         (Electron apps via Playwright CDP)
  Layer 3  →  Vision loop            (screenshot → LLM → action, always works)

The first layer that succeeds returns its result.  If all layers fail the
skill returns the combined error from every attempt.

metadata contract (unified — replaces cua_hotkey / cua_electron / cua_game):
  goal        (str, required)  — plain-English task description
  app         (str, optional)  — app name ("Calculator") or .app path
                                 ("…/Visual Studio Code.app").  Required for
                                 Layers 1 & 2; ignored for pure browser tasks.
  url         (str, optional)  — set this for browser / game tasks.
                                 When present, skips to Layer 3 directly.
  steps       (list, optional) — pre-planned osascript step list for Layer 1
                                 fast-path.  If omitted, Layer 1 is skipped.
  read_ax     (str, optional)  — AppleScript AX path to read a result value
                                 after Layer 1 steps complete.
  workspace   (str, optional)  — folder to open in Electron editors (Layer 2).
                                 Default: /Users/saikiran/Sandbox
  max_turns   (int, optional)  — vision loop turn budget (Layer 3). Default 10.
  keys        (list, optional) — allowed keys for browser game loop (Layer 3).
  click_x/y   (int, optional)  — pixel to click for game focus (Layer 3).
  gateway_url (str, optional)  — V9 gateway. Default http://localhost:8109
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from browser.client import V9Client
from schemas import AgentResult, NodeSpec


_DEFAULT_GATEWAY = "http://localhost:8109"
_DEFAULT_KEYS    = ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]

# ── known Electron apps (matched as substrings of the app path/name) ──────────
# Sublime Text is C++/Python — NOT Electron. Do not add it here.
_ELECTRON_APPS = (
    "visual studio code", "vs code", "vscode", "code",
    "atom", "slack", "discord",
    "figma", "notion", "obsidian", "cursor",
    "antigravity",
)

# ── key name → AppleScript key code ──────────────────────────────────────────
_KEY_CODES: dict[str, int] = {
    "return": 36, "enter": 36, "escape": 53, "tab": 48, "space": 49,
    "delete": 51, "backspace": 51, "up": 126, "down": 125,
    "left": 123, "right": 124, "end": 119, "home": 115,
    "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96,  "f6": 97,  "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
_MODIFIER_MAP: dict[str, str] = {
    "command": "command down", "cmd": "command down",
    "shift": "shift down", "option": "option down",
    "alt": "option down", "control": "control down", "ctrl": "control down",
}

# ── Layer 3 vision prompts ────────────────────────────────────────────────────
_BROWSER_GAME_SYSTEM = """\
You are a game-playing AI agent. Each turn you receive a raw screenshot of a
browser game. Analyze the visual state and decide the best keyboard move.

For 2048:
  Controls: ArrowUp / ArrowDown / ArrowLeft / ArrowRight
  Strategy: keep the highest tile in one corner; prefer merging moves.

IMPORTANT: Always output a valid "key" — one of the four arrow keys.
Do NOT confuse cookie/consent banners with Game Over.

A real Game Over screen has large "Game Over" text and a "Try again" button.

Output JSON only:
{
  "thinking":   "<board analysis and reason>",
  "key":        "<one of ArrowUp, ArrowDown, ArrowLeft, ArrowRight>",
  "game_state": "<highest tile, approximate score>",
  "done":       <true only on real Game Over or goal achieved>,
  "done_note":  "<why done>"
}
"""

_DESKTOP_VISION_SYSTEM = """\
You are a computer-use AI agent. Each turn you receive a screenshot of the
current macOS desktop state. Analyze what you see and decide the next action.

Available action types:
  keystroke   — type a string of characters into the focused app
  key_combo   — press a key with modifiers (e.g. Cmd+S, Cmd+Shift+P)
  shell       — run a shell command (e.g. to open an app, create a file)
  done        — task is complete or permanently failed

Output JSON only:
{
  "thinking":  "<1-2 sentences: what you see and why you chose this action>",
  "action": {
    "type":      "<keystroke | key_combo | shell | done>",
    "app":       "<macOS app name for osascript, e.g. Calculator>",
    "value":     "<text to type, key name, or shell command>",
    "modifiers": ["command", "shift"],
    "success":   <true | false>,
    "note":      "<why done>"
  },
  "done": <true | false>
}

For key_combo, use named keys: return, escape, s, end, home, pageup, …
Modifiers: command, shift, option, control

Set done=true when:
  • The goal has been fully achieved and is visible in the screenshot.
  • The task is permanently impossible (e.g. app not found).
"""

_BROWSER_GAME_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "key", "game_state", "done"],
    "properties": {
        "thinking":   {"type": "string"},
        "key":        {
            "type": "string",
            "enum": ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"],
        },
        "game_state": {"type": "string"},
        "done":       {"type": "boolean"},
        "done_note":  {"type": "string"},
    },
}

_DESKTOP_VISION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "action", "done"],
    "properties": {
        "thinking": {"type": "string"},
        "action": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type"],
            "properties": {
                "type":      {"type": "string",
                              "enum": ["keystroke", "key_combo", "shell", "done"]},
                "app":       {"type": "string"},
                "value":     {"type": "string"},
                "modifiers": {"type": "array", "items": {"type": "string"}},
                "success":   {"type": "boolean"},
                "note":      {"type": "string"},
            },
        },
        "done": {"type": "boolean"},
    },
}


# ─────────────────────────────────────────────────────────────────────────────

class ComputerSkill:
    NAME = "cua_computer"

    async def run(self, node: NodeSpec) -> AgentResult:
        meta        = node.metadata or {}
        goal        = meta.get("goal", "")
        app         = (meta.get("app") or meta.get("app_name")
                       or meta.get("app_path") or "")
        url         = meta.get("url", "")
        steps       = meta.get("steps") or []
        read_ax     = meta.get("read_ax")
        workspace   = meta.get("workspace") or "/Users/saikiran/Sandbox"
        max_turns   = int(meta.get("max_turns") or 10)
        keys        = meta.get("keys") or _DEFAULT_KEYS
        click_x     = int(meta.get("click_x") or 300)
        click_y     = int(meta.get("click_y") or 350)
        gateway_url = meta.get("gateway_url") or _DEFAULT_GATEWAY
        t0          = time.time()

        if not goal:
            return AgentResult(
                success=False, agent_name=self.NAME,
                error="metadata.goal is required",
                elapsed_s=time.time() - t0,
            )

        errors: list[str] = []

        # ── URL present → skip to Layer 3 (browser/game vision) ──────────────
        if url:
            print(f"[cua_computer] URL detected → Layer 3 (browser vision)")
            result = await self._layer3_browser(
                url, goal, max_turns, keys, click_x, click_y, gateway_url, t0,
            )
            if result.success:
                return result
            errors.append(f"Layer 3 browser: {result.error}")
            return _fail(self.NAME, errors, t0)

        # ── Layer 1: Hotkey / osascript ───────────────────────────────────────
        if app and not steps and not _is_electron(app):
            # Planner forgot to include steps — generate them from the goal.
            print(f"[cua_computer] Layer 1: no steps provided, auto-generating …")
            steps, read_ax = await _generate_steps(goal, app, gateway_url)
            if steps:
                print(f"[cua_computer] Layer 1: generated {len(steps)} steps")
            else:
                print(f"[cua_computer] Layer 1: step generation failed, skipping")

        if app and steps:
            print(f"[cua_computer] Layer 1 (hotkey) app={app!r} steps={len(steps)}")
            result = await self._layer1_hotkey(app, steps, read_ax, t0)
            if result.success:
                return result
            errors.append(f"Layer 1 hotkey: {result.error}")
            print(f"[cua_computer] Layer 1 failed — {result.error}")
        else:
            print(f"[cua_computer] Layer 1 skipped (no steps)")

        # ── Layer 2: Electron / CDP ───────────────────────────────────────────
        if app and _is_electron(app):
            print(f"[cua_computer] Layer 2 (electron/CDP) app={app!r}")
            result = await self._layer2_electron(app, goal, workspace, gateway_url, t0)
            if result.success:
                return result
            errors.append(f"Layer 2 electron: {result.error}")
            print(f"[cua_computer] Layer 2 failed — {result.error}")
        else:
            reason = "no app" if not app else "not an Electron app"
            print(f"[cua_computer] Layer 2 skipped ({reason})")

        # ── Layer 3: Vision (desktop screenshot loop) ─────────────────────────
        print(f"[cua_computer] Layer 3 (desktop vision)")
        result = await self._layer3_desktop(goal, app, gateway_url, max_turns, t0)
        if result.success:
            return result
        errors.append(f"Layer 3 desktop: {result.error}")

        return _fail(self.NAME, errors, t0)

    # ── Layer 1: osascript / hotkey ───────────────────────────────────────────

    async def _layer1_hotkey(
        self, app: str, steps: list, read_ax: str | None, t0: float,
    ) -> AgentResult:
        try:
            await _osascript(f'tell application "{app}" to activate')
            await asyncio.sleep(1.0)
        except RuntimeError as e:
            return AgentResult(
                success=False, agent_name=self.NAME,
                error=f"activate {app!r} failed: {e}",
                elapsed_s=time.time() - t0,
            )

        for i, step in enumerate(steps):
            action = step.get("action", "")
            value  = str(step.get("value", ""))
            mods   = step.get("modifiers") or []
            try:
                if action == "delay":
                    await asyncio.sleep(max(0.0, float(value) if value else 0.3))
                else:
                    script = _build_osascript(app, action, value, mods)
                    await _osascript(script)
                    await asyncio.sleep(0.1)
                print(f"[cua_computer] L1 step {i+1}/{len(steps)}: {action}({value!r}) ok")
            except Exception as e:
                return AgentResult(
                    success=False, agent_name=self.NAME,
                    error=f"L1 step {i+1} ({action}={value!r}): {e}",
                    elapsed_s=time.time() - t0,
                )

        result_value: str | None = None
        probes = ([read_ax] if read_ax else []) + _AX_FALLBACKS.get(app, [])
        for probe in probes:
            try:
                script = (
                    f'tell application "System Events"\n'
                    f'  tell process "{app}"\n'
                    f'    return {probe}\n'
                    f'  end tell\n'
                    f'end tell'
                )
                result_value = (await _osascript(script)).strip()
                print(f"[cua_computer] L1 read_ax → {result_value!r}")
                break
            except Exception:
                pass

        return AgentResult(
            success=True, agent_name=self.NAME,
            output={"layer": 1, "app": app, "steps_executed": len(steps),
                    "result": result_value},
            elapsed_s=time.time() - t0,
        )

    # ── Layer 2: Electron / CDP ───────────────────────────────────────────────

    async def _layer2_electron(
        self, app: str, goal: str, workspace: str, gateway_url: str, t0: float,
    ) -> AgentResult:
        proc = await _electron_launch(app, 9222, workspace)
        try:
            await _wait_for_cdp(9222)
        except RuntimeError as e:
            proc.terminate()
            return AgentResult(
                success=False, agent_name=self.NAME,
                error=str(e), elapsed_s=time.time() - t0,
            )

        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp("http://localhost:9222")
                page = await _find_workbench_page(browser)
                if page is None:
                    return AgentResult(
                        success=False, agent_name=self.NAME,
                        error="no workbench page found via CDP",
                        elapsed_s=time.time() - t0,
                    )

                # Pre-flight: dismiss first-launch chrome and Welcome tab.
                for _ in range(3):
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.4)
                await page.keyboard.press("Meta+w")
                await asyncio.sleep(0.8)

                client = V9Client(base_url=gateway_url, agent="cua_computer")
                filename, content = await _extract_file_plan(goal, client)
                print(f"[cua_computer] L2 plan: filename={filename!r} content={content!r}")
                ok = await _create_and_save(page, filename, content)
                if not ok.get("success"):
                    return AgentResult(
                        success=False, agent_name=self.NAME,
                        error=ok.get("note", "L2 scripted create failed"),
                        elapsed_s=time.time() - t0,
                    )
                return AgentResult(
                    success=True, agent_name=self.NAME,
                    output={"layer": 2, **ok},
                    elapsed_s=time.time() - t0,
                )
        except Exception as e:
            return AgentResult(
                success=False, agent_name=self.NAME,
                error=f"L2 CDP session: {e}",
                elapsed_s=time.time() - t0,
            )
        finally:
            proc.terminate()

    # ── Layer 3a: browser game vision loop ────────────────────────────────────

    async def _layer3_browser(
        self, url: str, goal: str, max_turns: int, keys: list[str],
        click_x: int, click_y: int, gateway_url: str, t0: float,
    ) -> AgentResult:
        client = V9Client(base_url=gateway_url, agent="cua_computer")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            ctx  = await browser.new_context(
                viewport={"width": 600, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                print(f"[cua_computer] L3-browser loaded: {page.url}")
                await asyncio.sleep(2.5)
                await _dismiss_popups(page)
                await page.mouse.click(click_x, click_y)
                await asyncio.sleep(0.5)
                print(f"[cua_computer] L3-browser focus at ({click_x},{click_y})")

                moves_log: list[dict] = []
                for turn in range(1, max_turns + 1):
                    raw_png  = await page.screenshot(full_page=False)
                    data_url = _png_to_data_url(raw_png)
                    recent   = " → ".join(m["key"] for m in moves_log[-5:]) or "(none)"
                    prompt   = (
                        f"GOAL: {goal}\nTURN: {turn}/{max_turns}\n"
                        f"RECENT MOVES: {recent}\n"
                        f"ALLOWED KEYS: {', '.join(keys)}\n\n"
                        f"Analyze the screenshot and choose the next move."
                    )
                    print(f"[cua_computer] L3-browser turn {turn}/{max_turns}: calling vision …")
                    try:
                        vr = await client.vision(
                            data_url, prompt,
                            system=_BROWSER_GAME_SYSTEM,
                            schema=_BROWSER_GAME_SCHEMA,
                            schema_name="game_action",
                            max_tokens=512,
                        )
                        decision = vr.parsed or {}
                    except Exception as e:
                        print(f"[cua_computer] L3-browser vision error: {e}")
                        break

                    key        = decision.get("key", "").strip()
                    game_state = decision.get("game_state", "")
                    done       = bool(decision.get("done", False))
                    print(f"[cua_computer] L3-browser turn {turn}: key={key!r} "
                          f"state={game_state!r} done={done}")

                    moves_log.append({"turn": turn, "key": key,
                                      "game_state": game_state, "done": done})
                    if done:
                        break
                    if key not in keys:
                        key = random.choice(keys)
                        print(f"[cua_computer] L3-browser key invalid → fallback {key!r}")
                    await page.keyboard.press(key)
                    await asyncio.sleep(0.35)

                # Final summary
                try:
                    final_png = await page.screenshot(full_page=False)
                    sr = await client.vision(
                        _png_to_data_url(final_png),
                        "Describe the final game state: highest tile, score, game over?",
                        system="You are a game state reporter. Answer in 1-2 sentences.",
                        max_tokens=150,
                    )
                    summary = sr.text.strip()
                except Exception as e:
                    summary = f"(summary unavailable: {e})"

                return AgentResult(
                    success=True, agent_name=self.NAME,
                    output={"layer": 3, "mode": "browser",
                            "turns_played": len(moves_log),
                            "moves_log": moves_log,
                            "final_summary": summary},
                    elapsed_s=time.time() - t0,
                )
            finally:
                await browser.close()

    # ── Layer 3b: desktop vision loop (screencapture + osascript) ─────────────

    async def _layer3_desktop(
        self, goal: str, app: str, gateway_url: str, max_turns: int, t0: float,
    ) -> AgentResult:
        client = V9Client(base_url=gateway_url, agent="cua_computer")

        if app:
            try:
                await _osascript(f'tell application "{app}" to activate')
                await asyncio.sleep(1.5)
            except Exception as e:
                print(f"[cua_computer] L3-desktop activate {app!r}: {e} (continuing)")

        steps_log: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="cua_vision_") as tmpdir:
            for turn in range(1, max_turns + 1):
                # Take screenshot of the current screen.
                screen_path = os.path.join(tmpdir, f"screen_{turn}.png")
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "screencapture", "-x", screen_path,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                    with open(screen_path, "rb") as f:
                        data_url = _png_to_data_url(f.read())
                except Exception as e:
                    return AgentResult(
                        success=False, agent_name=self.NAME,
                        error=f"L3-desktop screencapture failed: {e}",
                        elapsed_s=time.time() - t0,
                    )

                recent = (
                    " → ".join(
                        f"{s['action']['type']}({s['action'].get('value','')})"
                        for s in steps_log[-3:]
                    ) or "(none)"
                )
                prompt = (
                    f"GOAL: {goal}\n"
                    f"TURN: {turn}/{max_turns}\n"
                    f"RECENT ACTIONS: {recent}\n\n"
                    f"Look at the screenshot and decide the next action."
                )

                print(f"[cua_computer] L3-desktop turn {turn}/{max_turns}: calling vision …")
                try:
                    vr = await client.vision(
                        data_url, prompt,
                        system=_DESKTOP_VISION_SYSTEM,
                        schema=_DESKTOP_VISION_SCHEMA,
                        schema_name="desktop_action",
                        max_tokens=512,
                    )
                    decision = vr.parsed or {}
                except Exception as e:
                    print(f"[cua_computer] L3-desktop vision error: {e}")
                    break

                thinking = decision.get("thinking", "")
                action   = decision.get("action", {})
                done     = bool(decision.get("done", False))
                atype    = action.get("type", "")
                aapp     = action.get("app", app)
                avalue   = action.get("value", "")
                amods    = action.get("modifiers") or []

                print(f"[cua_computer] L3-desktop turn {turn}: {atype}({avalue!r}) "
                      f"done={done}")
                steps_log.append({"turn": turn, "thinking": thinking[:80],
                                   "action": action, "done": done})

                if atype == "done" or done:
                    success = bool(action.get("success", True))
                    note    = action.get("note", "")
                    return AgentResult(
                        success=success, agent_name=self.NAME,
                        output={"layer": 3, "mode": "desktop",
                                "steps": steps_log, "note": note},
                        elapsed_s=time.time() - t0,
                    )

                # Execute the action via osascript.
                try:
                    script = _build_osascript(aapp, atype, avalue, amods)
                    await _osascript(script)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"[cua_computer] L3-desktop action failed: {e}")
                    steps_log[-1]["error"] = str(e)

        return AgentResult(
            success=False, agent_name=self.NAME,
            error=f"L3-desktop max_turns ({max_turns}) reached without done signal",
            output={"layer": 3, "mode": "desktop", "steps": steps_log},
            elapsed_s=time.time() - t0,
        )


# ── helpers ────────────────────────────────────────────────────────────────────

def _is_electron(app: str) -> bool:
    return any(k in app.lower() for k in _ELECTRON_APPS)


async def _generate_steps(
    goal: str, app: str, gateway_url: str,
) -> tuple[list[dict], str | None]:
    """Ask the LLM to produce an osascript step list from the goal + app name.
    Returns (steps, read_ax).  Falls back to ([], None) on any error.
    """
    schema = {
        "type": "object",
        "required": ["steps"],
        "additionalProperties": False,
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["action", "value"],
                    "additionalProperties": False,
                    "properties": {
                        "action":    {"type": "string",
                                      "enum": ["keystroke", "key", "key_combo",
                                               "open_file", "shell", "delay"]},
                        "value":     {"type": "string"},
                        "modifiers": {"type": "array",
                                      "items": {"type": "string"}},
                    },
                },
            },
            "read_ax": {"type": "string"},
        },
    }
    prompt = (
        f"You are an osascript automation planner for macOS.\n"
        f"App: {app!r}\n"
        f"Goal: {goal}\n\n"
        f"Return a JSON object with:\n"
        f"  'steps': list of osascript action dicts to accomplish the goal.\n"
        f"  'read_ax': optional AppleScript AX path to read a result value "
        f"(e.g. the Calculator display), or omit if not needed.\n\n"
        f"Step vocabulary:\n"
        f"  keystroke  — type a string of chars/operators\n"
        f"  key        — named key (return, escape, tab, end, home, up, down, "
        f"left, right, f1..f12) with optional modifiers\n"
        f"  key_combo  — char or named key + modifiers (command/shift/option/control)\n"
        f"  open_file  — open a POSIX file path in the app\n"
        f"  shell      — run a shell command\n"
        f"  delay      — pause N seconds (value is number as string)\n\n"
        f"Known AX paths:\n"
        f"  Calculator display: \"value of static text 1 of scroll area 2 of "
        f"group 1 of group 1 of splitter group 1 of group 1 of window 1\"\n\n"
        f"Example for 'compute 48*125 in Calculator':\n"
        f"  steps: [\n"
        f"    {{\"action\":\"keystroke\",\"value\":\"48*125=\"}},\n"
        f"    {{\"action\":\"delay\",\"value\":\"1\"}}\n"
        f"  ]\n"
        f"  read_ax: \"value of static text 1 of scroll area 2 of group 1 ...\"\n"
    )
    try:
        client = V9Client(base_url=gateway_url, agent="cua_computer")
        result = await client.chat(
            prompt=prompt, schema=schema,
            schema_name="hotkey_plan", max_tokens=512,
        )
        plan    = result.parsed or json.loads(result.text or "{}")
        steps   = plan.get("steps") or []
        read_ax = plan.get("read_ax") or None
        return steps, read_ax
    except Exception as e:
        print(f"[cua_computer] _generate_steps error: {e}")
        return [], None


def _fail(name: str, errors: list[str], t0: float) -> AgentResult:
    return AgentResult(
        success=False, agent_name=name,
        error="all layers failed:\n" + "\n".join(f"  • {e}" for e in errors),
        elapsed_s=time.time() - t0,
    )


def _using_clause(mods: list[str]) -> str:
    parts = [_MODIFIER_MAP[m.lower()] for m in mods if m.lower() in _MODIFIER_MAP]
    return f" using {{{', '.join(parts)}}}" if parts else ""


def _build_osascript(app: str, action: str, value: str, mods: list[str]) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    using   = _using_clause(mods)

    if action == "keystroke":
        return (
            f'tell application "System Events"\n'
            f'  tell process "{app}"\n'
            f'    keystroke "{escaped}"{using}\n'
            f'  end tell\n'
            f'end tell'
        )
    if action in ("key", "key_combo"):
        key_lower = value.lower()
        if key_lower in _KEY_CODES:
            code = _KEY_CODES[key_lower]
            return (
                f'tell application "System Events"\n'
                f'  tell process "{app}"\n'
                f'    key code {code}{using}\n'
                f'  end tell\n'
                f'end tell'
            )
        return (
            f'tell application "System Events"\n'
            f'  tell process "{app}"\n'
            f'    keystroke "{escaped}"{using}\n'
            f'  end tell\n'
            f'end tell'
        )
    if action == "open_file":
        return (
            f'tell application "{app}"\n'
            f'  open POSIX file "{escaped}"\n'
            f'  activate\n'
            f'end tell'
        )
    if action == "shell":
        return f'do shell script "{escaped}"'
    raise ValueError(f"unknown action: {action!r}")


async def _osascript(script: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode().strip() or f"osascript exit {proc.returncode}")
    return stdout.decode()


_AX_FALLBACKS: dict[str, list[str]] = {
    "Calculator": [
        "value of static text 1 of scroll area 2 of group 1 of group 1 of splitter group 1 of group 1 of window 1",
        "value of static text 1 of scroll area 1 of group 1 of group 1 of splitter group 1 of group 1 of window 1",
    ],
    "TextEdit": [
        "value of text area 1 of scroll area 1 of window 1",
    ],
}


# ── Electron helpers (Layer 2) ────────────────────────────────────────────────

def _resolve_binary(app_path: str) -> str:
    import plistlib
    p = Path(app_path)
    if p.suffix != ".app":
        return app_path
    info_plist = p / "Contents" / "Info.plist"
    exe_name = "Electron"
    if info_plist.exists():
        with open(info_plist, "rb") as f:
            plist = plistlib.load(f)
        exe_name = plist.get("CFBundleExecutable", "Electron")
    return str(p / "Contents" / "MacOS" / exe_name)


async def _electron_launch(
    app_path: str, debug_port: int, workspace: str | None,
) -> subprocess.Popen:
    binary  = _resolve_binary(app_path)
    tmp_dir = Path(tempfile.mkdtemp(prefix="cua_electron_"))
    cmd     = [binary]

    if Path(binary).name == "Electron":
        app_resources = Path(binary).parent.parent / "Resources" / "app"
        cmd.append(str(app_resources))

    if workspace:
        ws_file = tmp_dir / "session.code-workspace"
        ws_file.write_text(json.dumps({"folders": [{"path": workspace}]}))
        cmd.append(str(ws_file))
        print(f"[cua_computer] L2 workspace={workspace!r}  via={ws_file}")

    cmd += [
        f"--remote-debugging-port={debug_port}",
        "--user-data-dir", str(tmp_dir),
        "--disable-workspace-trust",
        "--new-window",
    ]

    env = os.environ.copy()
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env.pop("ELECTRON_NO_ATTACH_CONSOLE", None)

    proc = subprocess.Popen(cmd, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[cua_computer] L2 launched pid={proc.pid}  port={debug_port}")
    await asyncio.sleep(4.0)
    return proc


async def _wait_for_cdp(port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(f"http://localhost:{port}/json")
                if r.status_code == 200:
                    print(f"[cua_computer] L2 CDP ready on :{port}")
                    return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError(f"CDP on :{port} not ready within {timeout}s")


async def _find_workbench_page(browser):
    for ctx in browser.contexts:
        for page in ctx.pages:
            if not any(s in page.url for s in
                       ("devtools://", "chrome-extension://", "about:blank", "chrome://")):
                print(f"[cua_computer] L2 workbench page: {page.url!r}")
                return page
    await asyncio.sleep(2.0)
    for ctx in browser.contexts:
        if ctx.pages:
            return ctx.pages[0]
    return None


async def _extract_file_plan(goal: str, client: V9Client) -> tuple[str, str]:
    import re as _re
    schema = {
        "type": "object",
        "required": ["filename", "content"],
        "additionalProperties": False,
        "properties": {
            "filename": {"type": "string"},
            "content":  {"type": "string"},
        },
    }
    try:
        result = await client.chat(
            prompt=(
                f"Extract filename and file content from this goal:\n\nGOAL: {goal}\n\n"
                f"Return JSON with 'filename' and 'content' only."
            ),
            schema=schema, schema_name="file_plan", max_tokens=256,
        )
        plan     = result.parsed or json.loads(result.text or "{}")
        filename = plan.get("filename", "").strip()
        content  = plan.get("content", "").strip()
        if filename and content:
            return filename, content
    except Exception:
        pass
    m = _re.search(r'\b([\w.-]+\.py)\b', goal)
    return (m.group(1) if m else "script.py"), 'print("Hello, World!")'


async def _create_and_save(page, filename: str, content: str) -> dict:
    workspace = "/Users/saikiran/Sandbox"
    full_path = f"{workspace}/{filename}"
    try:
        await page.keyboard.press("Meta+Shift+P")
        await asyncio.sleep(1.0)
        await page.keyboard.type("File: New File")
        await asyncio.sleep(0.5)
        await page.keyboard.press("ArrowDown")
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.8)
        await page.keyboard.type(filename)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.8)
        await page.keyboard.type(content)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Meta+s")
        await asyncio.sleep(1.0)
        print(f"[cua_computer] L2 saved {full_path!r}")
        await page.keyboard.press("Control+`")
        await asyncio.sleep(2.5)
        await page.keyboard.type(f"uv run python {filename}")
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")
        await asyncio.sleep(2.0)
        return {"success": True, "note": f"{filename} created at {full_path} and executed"}
    except Exception as e:
        return {"success": False, "note": f"scripted create failed: {e}"}


# ── browser game helpers (Layer 3a) ──────────────────────────────────────────

async def _dismiss_popups(page) -> None:
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.4)
    for sel in [
        ".fc-cta-consent", ".fc-button.fc-cta-consent", "button.fc-cta-consent",
        "button[id*='accept']", "button[class*='accept']",
        "button:has-text('Accept all')", "button:has-text('Accept All')",
        "button:has-text('Accept')", "button:has-text('I agree')",
        "button:has-text('Got it')", "button:has-text('OK')",
        "#onetrust-accept-btn-handler",
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=600):
                await btn.click()
                print(f"[cua_computer] dismissed popup via {sel!r}")
                await asyncio.sleep(0.8)
                break
        except Exception:
            pass
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.3)


def _png_to_data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode()
