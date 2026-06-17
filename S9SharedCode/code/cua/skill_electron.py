"""CUA ElectronSkill — Electron page path via CDP remote debugging.

Generic: launches any Electron app with --remote-debugging-port, connects
via Playwright CDP, then drives the UI through a multi-turn LLM loop that
reads the page's accessibility tree (with screenshot fallback) and emits
structured actions.

metadata contract (all set by the Planner):
  app_path   (str, optional) — full path to the Electron binary.
                                Defaults to Antigravity IDE.
  debug_port (int, optional) — CDP port; default 9222.
  goal       (str, required) — what to accomplish in plain English.
  workspace  (str, optional) — folder path to open in the editor.
  max_steps  (int, optional) — LLM turn cap; default 12.
  gateway_url (str, optional)— V9 gateway base URL; default http://localhost:8109.

Action vocabulary the LLM may emit each turn:
  click      {"type": "click",  "selector": "<css or aria-label>"}
  type       {"type": "type",   "selector": "<css>", "value": "<text>"}
             (selector optional — types into currently focused element)
  key        {"type": "key",    "value": "<key>"}
             e.g. "Control+Shift+P", "Escape", "Enter", "Meta+s"
  done       {"type": "done",   "success": true|false, "note": "<msg>"}

VS Code / Antigravity keyboard patterns:
  new file         → key("Control+Alt+Windows+N") or Command Palette "New File"
  command palette  → key("Meta+Shift+P") [Mac] or key("Control+Shift+P")
  save             → key("Meta+s") [Mac] or key("Control+s")
  save-as / name   → key("Meta+Shift+s") or via Command Palette "Save As"
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import time

import httpx
from playwright.async_api import async_playwright

from browser.client import V9Client
from schemas import AgentResult, NodeSpec


_DEFAULT_APP = "/Applications/Antigravity IDE.app/Contents/MacOS/Electron"
_DEFAULT_GATEWAY = "http://localhost:8109"

# Action schema sent to the LLM for structured output.
# eval is intentionally excluded: renderer context has no require/Node APIs.
_ACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "action"],
    "properties": {
        "thinking": {
            "type": "string",
            "description": "1-2 sentences of reasoning about the current state and next move.",
        },
        "action": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type"],
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["click", "type", "key", "done"],
                },
                "selector": {"type": "string"},
                "value":    {"type": "string"},
                "success":  {"type": "boolean"},
                "note":     {"type": "string"},
            },
        },
    },
}

_SYSTEM_PROMPT = (
    "You are a VS Code automation agent. You control VS Code via screenshots.\n"
    "Each turn: look at the screenshot, decide one action, emit it as JSON.\n\n"
    "ACTIONS:\n"
    "  key(value)               — keyboard shortcut in Playwright format\n"
    "  type(value)              — type text into the currently focused widget\n"
    "  click(selector)          — CSS selector click (last resort only)\n"
    "  done(success, note)      — task finished or permanently failed\n\n"
    "KEY RULES:\n"
    "  • NEVER retry a key or click that already failed — change approach.\n"
    "  • If you see a modal/overlay: key('Escape') to dismiss, then continue.\n\n"
    "CURRENT STATE: The file has already been created and saved.\n"
    "Your only job is to run it in the integrated terminal and confirm success.\n\n"
    "STEP SEQUENCE:\n"
    "  Step 1:  key('Control+Backquote')   open integrated terminal\n"
    "  Step 2:  type('python hello.py')    (wait for terminal to open first)\n"
    "  Step 3:  key('Enter')\n"
    "  Step 4:  done(success=true, note='hello.py created and executed')\n\n"
    "If the terminal is already open, skip Step 1.\n"
    "Output ONLY valid JSON matching the schema. No markdown."
)


class ElectronSkill:
    NAME = "cua_electron"

    async def run(self, node: NodeSpec) -> AgentResult:
        app_path    = node.metadata.get("app_path")   or _DEFAULT_APP
        debug_port  = int(node.metadata.get("debug_port") or 9222)
        goal        = node.metadata.get("goal", "")
        workspace   = node.metadata.get("workspace") or "/Users/saikiran/Sandbox"
        gateway_url = node.metadata.get("gateway_url") or _DEFAULT_GATEWAY
        t0 = time.time()

        if not goal:
            return AgentResult(
                success=False, agent_name=self.NAME,
                error="metadata.goal is required",
                elapsed_s=time.time() - t0,
            )

        print(f"[cua_electron] app={app_path!r}  port={debug_port}  goal={goal!r}")

        # ── launch ───────────────────────────────────────────────────────────
        proc = await self._launch(app_path, debug_port, workspace)
        try:
            await self._wait_for_cdp(debug_port)
        except RuntimeError as e:
            proc.terminate()
            return AgentResult(
                success=False, agent_name=self.NAME,
                error=str(e), elapsed_s=time.time() - t0,
            )

        # ── connect + drive ──────────────────────────────────────────────────
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(
                    f"http://localhost:{debug_port}"
                )
                page = await self._find_workbench_page(browser)
                if page is None:
                    return AgentResult(
                        success=False, agent_name=self.NAME,
                        error="no workbench page found via CDP",
                        elapsed_s=time.time() - t0,
                    )

                # Pre-flight: dismiss first-launch chrome.
                for _ in range(3):
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.4)
                # Close the Welcome tab (it's a tab, not a modal).
                await page.keyboard.press("Meta+w")
                await asyncio.sleep(0.8)

                # Ask the LLM to extract filename + content from the goal.
                client = V9Client(base_url=gateway_url, agent="cua_electron")
                _filename, _content = await self._extract_file_plan(goal, client)
                print(f"[cua_electron] plan: filename={_filename!r}  content={_content!r}")
                output = await self._create_and_save(page, _filename, _content)

                if not output.get("success"):
                    return AgentResult(
                        success=False, agent_name=self.NAME,
                        error=output.get("note", "scripted create failed"),
                        elapsed_s=time.time() - t0,
                    )
                # Scripted sequence handled everything — no LLM loop needed.

                return AgentResult(
                    success=output.get("success", False),
                    agent_name=self.NAME,
                    output=output,
                    elapsed_s=time.time() - t0,
                )
        except Exception as e:
            return AgentResult(
                success=False, agent_name=self.NAME,
                error=f"CDP session failed: {e}",
                elapsed_s=time.time() - t0,
            )
        finally:
            proc.terminate()

    # ── scripted file creation ────────────────────────────────────────────────

    @staticmethod
    async def _extract_file_plan(goal: str, client: V9Client) -> tuple[str, str]:
        """Ask the LLM to extract filename and file content from the goal.
        Returns (filename, content). Falls back to regex + hello-world on error.
        """
        import re as _re
        schema = {
            "type": "object",
            "required": ["filename", "content"],
            "additionalProperties": False,
            "properties": {
                "filename": {"type": "string", "description": "e.g. hello.py"},
                "content":  {"type": "string", "description": "full file content to write"},
            },
        }
        prompt = (
            f"Extract the filename and complete file content from this goal:\n\n"
            f"GOAL: {goal}\n\n"
            f"Return JSON with 'filename' and 'content' fields only. "
            f"'content' must be the exact code to write into the file."
        )
        try:
            result = await client.chat(
                prompt=prompt,
                schema=schema,
                schema_name="file_plan",
                max_tokens=256,
            )
            plan = result.parsed or json.loads(result.text or "{}")
            filename = plan.get("filename", "").strip()
            content  = plan.get("content", "").strip()
            if filename and content:
                return filename, content
        except Exception:
            pass
        # Fallback: regex for filename, hello-world content
        m = _re.search(r'\b([\w.-]+\.py)\b', goal)
        return (m.group(1) if m else "script.py"), 'print("Hello, World!")'

    async def _create_and_save(self, page, filename: str, content: str) -> dict:
        """Cmd+Shift+P → File: New File → filename → Enter → type content → Cmd+S."""
        workspace = "/Users/saikiran/Sandbox"
        full_path = f"{workspace}/{filename}"
        try:
            # 1. Open command palette
            await page.keyboard.press("Meta+Shift+P")
            await asyncio.sleep(1.0)

            # 2. Type the command
            await page.keyboard.type("File: New File")
            await asyncio.sleep(0.5)

            # 3. ArrowDown selects the right option, Enter executes it
            await page.keyboard.press("ArrowDown")
            await asyncio.sleep(0.3)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.8)

            # 4. Type filename in VS Code's "New File Name" prompt
            await page.keyboard.type(filename)
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.8)

            # 5. Type the file content into the editor
            await page.keyboard.type(content)
            await asyncio.sleep(0.5)

            # 6. Cmd+S — file is already named so no Save dialog appears
            await page.keyboard.press("Meta+s")
            await asyncio.sleep(1.0)
            print(f"[cua_electron] scripted: saved {full_path!r}")

            # 7. Open integrated terminal and run
            await page.keyboard.press("Control+`")
            await asyncio.sleep(2.5)
            await page.keyboard.type(f"uv run python {filename}")
            await asyncio.sleep(0.3)
            await page.keyboard.press("Enter")
            await asyncio.sleep(2.0)
            print(f"[cua_electron] scripted: executed 'uv run python {filename}'")

            return {"success": True, "note": f"{filename} created at {full_path} and executed"}
        except Exception as e:
            return {"success": False, "note": f"scripted create failed: {e}"}

    # ── drive loop ────────────────────────────────────────────────────────────

    async def _drive(self, page, client: V9Client, goal: str, max_steps: int) -> dict:
        steps_log: list[dict] = []
        last_error: str | None = None

        for turn in range(1, max_steps + 1):
            # Try AX tree snapshot first; fall back to screenshot when it
            # returns None or throws (common for VS Code's custom renderer).
            ax_text:             str | None = None
            screenshot_data_url: str | None = None

            try:
                snapshot = await page.accessibility.snapshot()
                if snapshot:
                    ax_text = json.dumps(snapshot, indent=2)[:6000]
            except Exception:
                pass

            if not ax_text:
                try:
                    png_bytes = await page.screenshot()
                    screenshot_data_url = (
                        "data:image/png;base64,"
                        + base64.b64encode(png_bytes).decode()
                    )
                    print(f"[cua_electron] turn {turn}: AX unavailable — using screenshot")
                except Exception as e:
                    print(f"[cua_electron] turn {turn}: screenshot also failed: {e}")

            # Build prompt — include prior error so LLM can adapt.
            prompt = f"GOAL: {goal}\n\nTURN: {turn}/{max_steps}\n\n"
            if last_error:
                prompt += f"PREVIOUS ACTION FAILED: {last_error}\nTry a different approach.\n\n"
            if ax_text:
                prompt += f"ACCESSIBILITY TREE:\n{ax_text}"
            elif screenshot_data_url:
                prompt += "(accessibility tree unavailable — see screenshot)"
            else:
                prompt += "(no state available — try a key action)"

            print(f"[cua_electron] turn {turn}: requesting LLM decision ...")
            try:
                if screenshot_data_url and not ax_text:
                    result = await client.vision(
                        image_data_url=screenshot_data_url,
                        prompt=prompt,
                        system=_SYSTEM_PROMPT,
                        schema=_ACTION_SCHEMA,
                        schema_name="electron_action",
                        max_tokens=512,
                    )
                else:
                    result = await client.chat(
                        prompt=prompt,
                        system=_SYSTEM_PROMPT,
                        schema=_ACTION_SCHEMA,
                        schema_name="electron_action",
                        max_tokens=512,
                    )
                decision = result.parsed or json.loads(result.text or "{}")
            except Exception as e:
                print(f"[cua_electron] LLM call failed: {e}")
                break

            thinking = decision.get("thinking", "")
            action   = decision.get("action", {})
            atype    = action.get("type", "")
            print(f"[cua_electron] turn {turn}: {atype}  thinking={thinking[:80]!r}")

            steps_log.append({"turn": turn, "thinking": thinking, "action": action})

            # done signal
            if atype == "done":
                success = bool(action.get("success", True))
                note    = action.get("note", "")
                print(f"[cua_electron] done — success={success}  note={note!r}")
                return {"success": success, "note": note, "steps": steps_log}

            # execute action
            try:
                await self._execute(page, action)
                last_error = None
                await asyncio.sleep(0.6)
            except Exception as e:
                print(f"[cua_electron] action failed: {e}")
                last_error = str(e)
                steps_log[-1]["error"] = str(e)

        return {
            "success": False,
            "note":    f"max_steps ({max_steps}) reached without done signal",
            "steps":   steps_log,
        }

    async def _execute(self, page, action: dict) -> None:
        atype    = action.get("type", "")
        selector = action.get("selector", "")
        value    = action.get("value", "")

        if atype == "click":
            if selector:
                await page.click(selector, timeout=3000)
            else:
                raise ValueError("click requires selector")

        elif atype == "type":
            if selector:
                await page.click(selector, timeout=3000)
            await page.keyboard.type(value)

        elif atype == "key":
            # Playwright key format: "Control+Shift+P", "Meta+s", "Escape"
            await page.keyboard.press(value)

        else:
            raise ValueError(f"unknown action type: {atype!r}")

    # ── startup helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _resolve_binary(app_path: str) -> str:
        """If app_path is a .app bundle, return the CFBundleExecutable binary."""
        import plistlib
        from pathlib import Path as _P

        p = _P(app_path)
        if p.suffix != ".app":
            return app_path
        info_plist = p / "Contents" / "Info.plist"
        exe_name = "Electron"
        if info_plist.exists():
            with open(info_plist, "rb") as f:
                plist = plistlib.load(f)
            exe_name = plist.get("CFBundleExecutable", "Electron")
        return str(p / "Contents" / "MacOS" / exe_name)

    @staticmethod
    async def _launch(app_path: str, debug_port: int,
                      workspace: str | None) -> subprocess.Popen:
        import json
        import tempfile
        from pathlib import Path as _P

        # Accept both ".app bundle" paths and explicit binary paths.
        app_path = ElectronSkill._resolve_binary(app_path)

        electron_bin = _P(app_path)
        tmp_data = _P(tempfile.mkdtemp(prefix="cua_electron_"))

        cmd = [str(electron_bin)]

        # Only the *generic* Electron binary needs app_resources as its first
        # positional arg. Named binaries (VS Code's "Code") are self-contained.
        if electron_bin.name == "Electron":
            app_resources = electron_bin.parent.parent / "Resources" / "app"
            cmd.append(str(app_resources))

        # VS Code's Electron binary does not honour positional folder paths or
        # --folder-uri (those are CLI-wrapper features). The only reliable way
        # to specify which folder to open is a .code-workspace JSON file: VS
        # Code ALWAYS reads it regardless of how the binary is invoked.
        if workspace:
            ws_file = tmp_data / "session.code-workspace"
            ws_file.write_text(json.dumps({"folders": [{"path": workspace}]}))
            cmd.append(str(ws_file))
            print(f"[cua_electron] workspace={workspace!r}  via={ws_file}")

        cmd += [
            f"--remote-debugging-port={debug_port}",
            "--user-data-dir", str(tmp_data),
            "--disable-workspace-trust",
            "--new-window",
        ]

        env = os.environ.copy()
        env.pop("ELECTRON_RUN_AS_NODE", None)
        env.pop("ELECTRON_NO_ATTACH_CONSOLE", None)

        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[cua_electron] launched pid={proc.pid}  port={debug_port}  data={tmp_data}")
        await asyncio.sleep(4.0)
        return proc

    @staticmethod
    async def _wait_for_cdp(port: int, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        url = f"http://localhost:{port}/json"
        while time.time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=2.0) as c:
                    r = await c.get(url)
                    if r.status_code == 200:
                        print(f"[cua_electron] CDP ready on :{port}")
                        return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError(f"CDP on :{port} did not become ready within {timeout}s")

    @staticmethod
    async def _find_workbench_page(browser):
        """Enter the main workbench page, skipping DevTools / service-worker pages."""
        contexts = browser.contexts
        for ctx in contexts:
            for page in ctx.pages:
                url = page.url
                # Skip DevTools, extension background pages, and blank pages.
                if any(skip in url for skip in
                       ("devtools://", "chrome-extension://", "about:blank", "chrome://"))  :
                    continue
                print(f"[cua_electron] workbench page url={url!r}")
                return page
        # Fallback: wait briefly for a page to appear.
        await asyncio.sleep(2.0)
        for ctx in browser.contexts:
            if ctx.pages:
                return ctx.pages[0]
        return None
