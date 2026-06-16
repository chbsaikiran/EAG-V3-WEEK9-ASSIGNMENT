"""CUA HotkeySkill — Layer 2a: deterministic hotkey execution via osascript.

Generic: drives any macOS app by name using the step list the planner emits.
Zero LLM calls at runtime — purely deterministic keystroke dispatch.

metadata contract (all set by the Planner):
  app_name  (str, required)  — macOS application name, e.g. "Calculator"
  steps     (list, required) — ordered list of action dicts:

      {"action": "keystroke", "value": "<text>"}
          Type a string of characters including operators like +, *, /
          Escape sequences: use \\n for newline, \\t for tab.

      {"action": "key", "value": "<name>", "modifiers": [...]}
          Press a named special key, optionally with modifier keys.
          Supported names: return, escape, tab, space, delete, backspace,
                           up, down, left, right, f1..f12
          Modifiers: "command", "shift", "option", "control"
          Example: {"action": "key", "value": "return"}
          Example: {"action": "key", "value": "s", "modifiers": ["command"]}

      {"action": "key_combo", "value": "<char or named key>", "modifiers": ["command", ...]}
          Keystroke a character OR named key with modifier keys.
          Named keys (end, home, pageup, pagedown, return, …) use key code.
          Example: {"action": "key_combo", "value": "s", "modifiers": ["command"]}
          Example: {"action": "key_combo", "value": "end", "modifiers": ["command"]}

      {"action": "open_file", "value": "<posix file path>"}
          Open a file in the app using AppleScript's `open POSIX file`.
          Reliable for any known path — no file dialog needed.
          app_name can be a short name ("Sublime Text") or a full path
          ("/Users/x/Downloads/Sublime Text.app").
          Example: {"action": "open_file", "value": "/Users/saikiran/sai.txt"}

      {"action": "shell", "value": "<shell command>"}
          Run a shell command via `do shell script`.
          Use when no hotkey exists: create files, copy, run CLI tools.
          Example: {"action": "shell", "value": "touch /tmp/test.txt"}

      {"action": "delay", "value": <seconds as string or number>}
          Pause for the given number of seconds.

  app_name  can be:
    • A short macOS display name: "Calculator", "TextEdit", "Sublime Text"
    • A full .app path for apps NOT in /Applications:
      "/Users/saikiran/Downloads/Sublime Text.app"
      AppleScript accepts both forms.

  read_ax   (str, optional)  — AppleScript AX expression evaluated inside
                                the app's System Events process, e.g.
                                "value of static text 1 of window 1"
                                The result is returned in output.result.
                                If omitted, output.result is null.

Known AX paths:
  macOS Calculator display  → "value of static text 1 of scroll area 2 of group 1 of group 1 of splitter group 1 of group 1 of window 1"
  macOS TextEdit content    → "value of text area 1 of scroll area 1 of window 1"
"""
from __future__ import annotations

import asyncio
import time

from schemas import AgentResult, NodeSpec


# ── key name → AppleScript key code ─────────────────────────────────────────
# Key codes are layout-independent (physical position). Named keys are
# dispatched via `key code N`; printable chars via `keystroke "c"`.
_KEY_CODES: dict[str, int] = {
    "return":    36,
    "enter":     36,
    "escape":    53,
    "tab":       48,
    "space":     49,
    "delete":    51,
    "backspace": 51,
    "up":       126,
    "down":     125,
    "left":     123,
    "right":    124,
    "end":      119,
    "home":     115,
    "pageup":   116,
    "pagedown": 121,
    "f1": 122, "f2": 120, "f3":  99, "f4": 118,
    "f5":  96, "f6":  97, "f7":  98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

# Known AX paths for reading the result display of common apps.
# Tried in order when the planner-supplied read_ax path fails or is absent.
_AX_FALLBACKS: dict[str, list[str]] = {
    "Calculator": [
        # macOS Sonoma / Ventura / Monterey — result (large number)
        "value of static text 1 of scroll area 2 of group 1 of group 1 of splitter group 1 of group 1 of window 1",
        # expression line (shows e.g. "125×48=")
        "value of static text 1 of scroll area 1 of group 1 of group 1 of splitter group 1 of group 1 of window 1",
    ],
    "TextEdit": [
        "value of text area 1 of scroll area 1 of window 1",
    ],
}

# AppleScript modifier names used in `using {… down}` clauses.
_MODIFIER_MAP: dict[str, str] = {
    "command": "command down",
    "cmd":     "command down",
    "shift":   "shift down",
    "option":  "option down",
    "alt":     "option down",
    "control": "control down",
    "ctrl":    "control down",
}


class HotkeySkill:
    NAME = "cua_hotkey"

    async def run(self, node: NodeSpec) -> AgentResult:
        app_name = node.metadata.get("app_name", "")
        steps    = node.metadata.get("steps", [])
        read_ax  = node.metadata.get("read_ax")
        t0 = time.time()

        if not app_name:
            return AgentResult(
                success=False, agent_name=self.NAME,
                error="metadata.app_name is required",
                elapsed_s=time.time() - t0,
            )

        print(f"[cua_hotkey] app={app_name!r}  steps={len(steps)}  read_ax={read_ax!r}")

        # ── activate app ─────────────────────────────────────────────────────
        try:
            await _osascript(f'tell application "{app_name}" to activate')
            await asyncio.sleep(1.0)
        except RuntimeError as e:
            return AgentResult(
                success=False, agent_name=self.NAME,
                error=f"activate {app_name!r} failed: {e}",
                elapsed_s=time.time() - t0,
            )

        # ── execute steps ────────────────────────────────────────────────────
        for i, step in enumerate(steps):
            action = step.get("action", "")
            value  = str(step.get("value", ""))
            mods   = step.get("modifiers") or []

            try:
                if action == "delay":
                    await asyncio.sleep(max(0.0, float(value) if value else 0.3))

                elif action == "keystroke":
                    # Type a string — good for digits, operators, text.
                    escaped  = value.replace("\\", "\\\\").replace('"', '\\"')
                    using    = _using_clause(mods)
                    script   = (
                        f'tell application "System Events"\n'
                        f'  tell process "{app_name}"\n'
                        f'    keystroke "{escaped}"{using}\n'
                        f'  end tell\n'
                        f'end tell'
                    )
                    await _osascript(script)
                    await asyncio.sleep(0.05)

                elif action == "key":
                    # Named special key (return, escape, f5, …) or single char
                    # with optional modifiers.
                    key_lower = value.lower()
                    if key_lower in _KEY_CODES:
                        code  = _KEY_CODES[key_lower]
                        using = _using_clause(mods)
                        script = (
                            f'tell application "System Events"\n'
                            f'  tell process "{app_name}"\n'
                            f'    key code {code}{using}\n'
                            f'  end tell\n'
                            f'end tell'
                        )
                    else:
                        # Single printable character with modifiers.
                        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                        using   = _using_clause(mods)
                        script  = (
                            f'tell application "System Events"\n'
                            f'  tell process "{app_name}"\n'
                            f'    keystroke "{escaped}"{using}\n'
                            f'  end tell\n'
                            f'end tell'
                        )
                    await _osascript(script)
                    await asyncio.sleep(0.1)

                elif action == "key_combo":
                    # Character or named key + one or more modifiers.
                    # e.g. Cmd+S, Cmd+End, Cmd+Shift+P
                    key_lower = value.lower()
                    using     = _using_clause(mods)
                    if key_lower in _KEY_CODES:
                        code   = _KEY_CODES[key_lower]
                        script = (
                            f'tell application "System Events"\n'
                            f'  tell process "{app_name}"\n'
                            f'    key code {code}{using}\n'
                            f'  end tell\n'
                            f'end tell'
                        )
                    else:
                        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                        script  = (
                            f'tell application "System Events"\n'
                            f'  tell process "{app_name}"\n'
                            f'    keystroke "{escaped}"{using}\n'
                            f'  end tell\n'
                            f'end tell'
                        )
                    await _osascript(script)
                    await asyncio.sleep(0.15)

                elif action == "open_file":
                    # Open a file directly in the app using AppleScript.
                    # Works with both short names ("Sublime Text") and full
                    # paths ("/Users/x/Downloads/Sublime Text.app").
                    # Much more reliable than Spotlight or file-open dialogs.
                    escaped_path = value.replace("\\", "\\\\").replace('"', '\\"')
                    script = (
                        f'tell application "{app_name}"\n'
                        f'    open POSIX file "{escaped_path}"\n'
                        f'    activate\n'
                        f'end tell'
                    )
                    await _osascript(script)
                    await asyncio.sleep(1.5)   # let the file load in the editor

                elif action == "shell":
                    # Run an arbitrary shell command via AppleScript.
                    # Use for operations that have no direct hotkey equivalent
                    # e.g. creating a file, copying, running a CLI tool.
                    escaped_cmd = value.replace("\\", "\\\\").replace('"', '\\"')
                    script = f'do shell script "{escaped_cmd}"'
                    await _osascript(script)
                    await asyncio.sleep(0.3)

                else:
                    raise ValueError(f"unknown action: {action!r}")

                print(f"[cua_hotkey] step {i + 1}/{len(steps)}: "
                      f"{action}({value!r}{'' if not mods else ', mods=' + str(mods)}) ok")

            except Exception as e:
                print(f"[cua_hotkey] step {i + 1} FAILED: {e}")
                return AgentResult(
                    success=False, agent_name=self.NAME,
                    error=f"step {i + 1} ({action}={value!r}): {e}",
                    output={"app_name": app_name, "steps_executed": i, "result": None},
                    elapsed_s=time.time() - t0,
                )

        # ── read AX result ───────────────────────────────────────────────────
        result_value: str | None = None
        await asyncio.sleep(0.2)
        # Build the probe list: planner-supplied path first, then known
        # fallbacks so the skill never silently returns null on a bad path.
        ax_probes: list[str] = []
        if read_ax:
            ax_probes.append(read_ax)
        ax_probes += _AX_FALLBACKS.get(app_name, [])

        for probe in ax_probes:
            try:
                script = (
                    f'tell application "System Events"\n'
                    f'  tell process "{app_name}"\n'
                    f'    return {probe}\n'
                    f'  end tell\n'
                    f'end tell'
                )
                result_value = (await _osascript(script)).strip()
                print(f"[cua_hotkey] read_ax ({probe!r}) → {result_value!r}")
                break
            except Exception as e:
                print(f"[cua_hotkey] read_ax probe failed ({probe!r}): {e}")

        return AgentResult(
            success=True, agent_name=self.NAME,
            output={
                "app_name":       app_name,
                "steps_executed": len(steps),
                "result":         result_value,
            },
            elapsed_s=time.time() - t0,
        )


# ── helpers ───────────────────────────────────────────────────────────────────

def _using_clause(mods: list[str]) -> str:
    """Build AppleScript `using {command down, shift down}` clause or ''."""
    parts = [_MODIFIER_MAP[m.lower()] for m in mods if m.lower() in _MODIFIER_MAP]
    return f" using {{{', '.join(parts)}}}" if parts else ""


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
