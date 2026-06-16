"""CUA ElectronSkill — Electron page path via CDP remote debugging.

Generic: launches any Electron app with --remote-debugging-port, connects
via Playwright CDP, then drives the UI through a multi-turn LLM loop that
reads the page's accessibility tree and emits structured actions.

This mirrors the A11yDriver pattern used by the Browser skill but targets
desktop Electron apps (Antigravity, VS Code, Slack, Notion, etc.) rather
than web pages.

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
  eval       {"type": "eval",   "js": "<javascript expression>"}
             result is logged; use for state reads or complex DOM ops.
  done       {"type": "done",   "success": true|false, "note": "<msg>"}
"""
from __future__ import annotations

import asyncio
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
                    "enum": ["click", "type", "key", "eval", "done"],
                },
                "selector": {"type": "string"},
                "value":    {"type": "string"},
                "js":       {"type": "string"},
                "success":  {"type": "boolean"},
                "note":     {"type": "string"},
            },
        },
    },
}

_SYSTEM_PROMPT = (
    "You are an Electron desktop-app automation agent. "
    "Each turn you receive the current page accessibility tree (JSON) and the goal. "
    "Emit exactly one action to make progress. Available actions:\n"
    "  click(selector)          — click element by CSS selector or aria-label\n"
    "  type(selector?, value)   — type text; omit selector to use focused element\n"
    "  key(value)               — keyboard shortcut e.g. 'Control+Shift+P', 'Escape'\n"
    "  eval(js)                 — execute JavaScript in the page; result is logged\n"
    "  done(success, note)      — signal task complete or permanently failed\n"
    "Output ONLY the JSON object matching the schema. No markdown."
)


class ElectronSkill:
    NAME = "cua_electron"

    async def run(self, node: NodeSpec) -> AgentResult:
        app_path    = node.metadata.get("app_path")   or _DEFAULT_APP
        debug_port  = int(node.metadata.get("debug_port") or 9222)
        goal        = node.metadata.get("goal", "")
        workspace   = node.metadata.get("workspace")
        max_steps   = int(node.metadata.get("max_steps") or 12)
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

                client = V9Client(base_url=gateway_url, agent="cua_electron")
                output = await self._drive(page, client, goal, max_steps)

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

    # ── drive loop ────────────────────────────────────────────────────────────

    async def _drive(self, page, client: V9Client, goal: str, max_steps: int) -> dict:
        steps_log: list[dict] = []

        for turn in range(1, max_steps + 1):
            # Snapshot the AX tree.
            try:
                snapshot = await page.accessibility.snapshot()
                ax_text  = json.dumps(snapshot, indent=2)[:6000]
            except Exception as e:
                ax_text = f"(ax_snapshot_failed: {e})"

            prompt = (
                f"GOAL: {goal}\n\n"
                f"TURN: {turn}/{max_steps}\n\n"
                f"ACCESSIBILITY TREE:\n{ax_text}"
            )

            print(f"[cua_electron] turn {turn}: requesting LLM decision ...")
            try:
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
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[cua_electron] action failed: {e}")
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
        js       = action.get("js", "")

        if atype == "click":
            if selector:
                await page.click(selector, timeout=8000)
            else:
                raise ValueError("click requires selector")

        elif atype == "type":
            if selector:
                await page.click(selector, timeout=8000)
            await page.keyboard.type(value)

        elif atype == "key":
            # Playwright key format: "Control+Shift+P", "Meta+s", "Escape"
            await page.keyboard.press(value)

        elif atype == "eval":
            result = await page.evaluate(js)
            print(f"[cua_electron] eval result: {str(result)[:200]!r}")

        else:
            raise ValueError(f"unknown action type: {atype!r}")

    # ── startup helpers ───────────────────────────────────────────────────────

    @staticmethod
    async def _launch(app_path: str, debug_port: int,
                      workspace: str | None) -> subprocess.Popen:
        import tempfile
        from pathlib import Path as _P

        electron_bin = _P(app_path)
        # For VS Code forks: the Electron binary needs the app resources
        # directory as its first positional argument. Without it the binary
        # opens a blank Electron window and never exposes a CDP endpoint.
        app_resources = electron_bin.parent.parent / "Resources" / "app"

        # Force an independent instance even when the app is already open.
        # VS Code forks normally delegate a second launch to the running
        # instance and exit immediately — that process never exposes CDP.
        # A unique --user-data-dir bypasses the single-instance lock.
        tmp_data = tempfile.mkdtemp(prefix="cua_electron_")

        cmd = [
            str(electron_bin),
            str(app_resources),
            f"--remote-debugging-port={debug_port}",
            "--user-data-dir", tmp_data,
            "--disable-workspace-trust",  # skip trust dialog on open
        ]
        if workspace:
            cmd.append(workspace)

        env = os.environ.copy()
        # When running inside an Electron app (VS Code / Antigravity extension
        # host), ELECTRON_RUN_AS_NODE=1 is set by the extension host launcher.
        # If it leaks into our subprocess env the Electron binary starts as a
        # plain Node.js process and crashes with "does not provide an export
        # named 'app'". Unsetting it lets Electron start as a GUI app.
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
        """Return the main workbench page, skipping DevTools / service-worker pages."""
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
