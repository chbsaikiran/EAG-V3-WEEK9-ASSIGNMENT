"""CUA BrowserGameSkill — Layer 3 vision for canvas-rendered browser games."""
from __future__ import annotations

import asyncio
import base64
import random
import time

from playwright.async_api import async_playwright

from browser.client import V9Client
from schemas import AgentResult, NodeSpec


_DEFAULT_GATEWAY = "http://localhost:8109"
_DEFAULT_KEYS    = ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]

# ── vision system prompt — game strategy, not web navigation ─────────────────
_GAME_SYSTEM_PROMPT = """\
You are a game-playing AI agent. Each turn you receive a raw screenshot of a
browser game. Analyze the visual state and decide the best keyboard move.

For 2048:
  Board: 4×4 grid of numbered tiles. Tiles merge when two equal numbers meet.
  Controls: ArrowUp / ArrowDown / ArrowLeft / ArrowRight
  Strategy:
    • Keep the highest tile in one corner (e.g., bottom-left).
    • Build along the bottom edge so the corner tile can keep merging.
    • Prefer moves that merge the most tiles in one sweep.
    • Never make a move that boxes in your highest tile with no exit.
  Game over: when no move changes the board and no empty cells remain.

IMPORTANT: You MUST always output a valid "key" field — one of:
  ArrowUp  ArrowDown  ArrowLeft  ArrowRight

Never output an empty string for "key". Even if unsure, pick the best arrow key.

POPUPS AND OVERLAYS — do NOT confuse these with Game Over:
  • Cookie consent banners, GDPR notices, "Accept cookies" dialogs → NOT game over.
  • App install prompts, "Add to home screen" banners → NOT game over.
  • Any dialog that does not say "Game Over" in large text → NOT game over.
  If you see a popup/overlay obscuring the game, set done=false and pick a move
  anyway — the game code will handle dismissal.

A real Game Over screen has LARGE "Game Over" text plus a "Try again" button.

Output JSON only — no markdown, no explanation outside the JSON:
{
  "thinking":   "<2–3 sentences: board analysis and reason for chosen move>",
  "key":        "<REQUIRED: one of ArrowUp, ArrowDown, ArrowLeft, ArrowRight>",
  "game_state": "<brief: highest tile, approximate score if visible>",
  "done":       <true | false>,
  "done_note":  "<why done, e.g. 'completed 10 moves' or 'game over detected'>"
}

Set done=true ONLY when:
  • The screenshot clearly shows "Game Over" text with a "Try again" button.
  • The goal tile has been reached (e.g., 2048 tile achieved).
  NOTE: Reaching max_turns does NOT set done — the orchestrator handles that.
"""

# ── action schema — key is required with an enum to force a valid move ────────
_GAME_ACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "key", "game_state", "done"],
    "properties": {
        "thinking":   {"type": "string"},
        "key":        {
            "type": "string",
            "enum": ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"],
            "description": "The keyboard key to press. MUST be one of the four arrow keys.",
        },
        "game_state": {"type": "string"},
        "done":       {"type": "boolean"},
        "done_note":  {"type": "string"},
    },
}


class BrowserGameSkill:
    NAME = "cua_game"

    async def run(self, node: NodeSpec) -> AgentResult:
        url         = node.metadata.get("url", "")
        goal        = node.metadata.get("goal", "play the game for the allowed turns")
        max_turns   = int(node.metadata.get("max_turns") or 10)
        allowed_keys = node.metadata.get("keys") or _DEFAULT_KEYS
        click_x     = int(node.metadata.get("click_x") or 300)
        click_y     = int(node.metadata.get("click_y") or 350)
        provider    = node.metadata.get("provider_pin")
        gateway_url = node.metadata.get("gateway_url") or _DEFAULT_GATEWAY
        t0 = time.time()

        if not url:
            return AgentResult(
                success=False, agent_name=self.NAME,
                error="metadata.url is required",
                elapsed_s=time.time() - t0,
            )

        print(f"[cua_game] url={url!r}  goal={goal!r}  max_turns={max_turns}")

        client = V9Client(base_url=gateway_url, agent="cua_game")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            ctx = await browser.new_context(
                viewport={"width": 600, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await ctx.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                print(f"[cua_game] page loaded: {page.url}")
                await asyncio.sleep(2.5)          # let game JS initialise

                # ── dismiss cookie/consent banners ────────────────────────────
                await _dismiss_popups(page)

                # Re-click game board to give it keyboard focus.
                await page.mouse.click(click_x, click_y)
                await asyncio.sleep(0.5)
                print(f"[cua_game] keyboard focus acquired at ({click_x}, {click_y})")

                moves_log: list[dict] = []

                for turn in range(1, max_turns + 1):
                    # ── raw screenshot — no SoM annotation ───────────────────
                    raw_png  = await page.screenshot(full_page=False)
                    data_url = _png_to_data_url(raw_png)

                    recent = " → ".join(m["key"] for m in moves_log[-5:]) or "(none yet)"
                    prompt  = (
                        f"GOAL: {goal}\n\n"
                        f"TURN: {turn}/{max_turns}\n"
                        f"RECENT MOVES: {recent}\n"
                        f"ALLOWED KEYS: {', '.join(allowed_keys)}\n\n"
                        f"Analyze the screenshot and choose the next move."
                    )

                    print(f"[cua_game] turn {turn}/{max_turns}: calling vision ...")
                    try:
                        vr      = await client.vision(
                            data_url, prompt,
                            system=_GAME_SYSTEM_PROMPT,
                            schema=_GAME_ACTION_SCHEMA,
                            schema_name="game_action",
                            max_tokens=512,
                            provider=provider,
                        )
                        decision = vr.parsed or {}
                    except Exception as e:
                        print(f"[cua_game] vision call failed: {e}")
                        break

                    thinking   = decision.get("thinking", "")
                    key        = decision.get("key", "").strip()
                    game_state = decision.get("game_state", "")
                    done       = bool(decision.get("done", False))
                    done_note  = decision.get("done_note", "")

                    print(f"[cua_game] turn {turn}: key={key!r}  "
                          f"state={game_state!r}  done={done}")

                    moves_log.append({
                        "turn": turn, "key": key,
                        "game_state": game_state,
                        "thinking": thinking[:120],
                        "done": done, "done_note": done_note,
                    })

                    if done:
                        print(f"[cua_game] done signal received: {done_note!r}")
                        break

                    # Fallback: if model returned invalid/empty key, pick randomly.
                    if key not in allowed_keys:
                        key = random.choice(allowed_keys)
                        print(f"[cua_game] key invalid — using fallback {key!r}")

                    await page.keyboard.press(key)
                    await asyncio.sleep(0.35)   # let tile animation settle

                # ── final board summary ───────────────────────────────────────
                final_png = await page.screenshot(full_page=False)
                final_url = _png_to_data_url(final_png)
                try:
                    sr = await client.vision(
                        final_url,
                        "Describe the final game state: highest tile value, "
                        "visible score, and whether the game is over.",
                        system=(
                            "You are a game state reporter. Look at the screenshot "
                            "and answer concisely in 1–2 sentences."
                        ),
                        max_tokens=150,
                        provider=provider,
                    )
                    final_summary = sr.text.strip()
                except Exception as e:
                    final_summary = f"(summary unavailable: {e})"

                print(f"[cua_game] finished — turns={len(moves_log)}  "
                      f"summary={final_summary!r}")

                return AgentResult(
                    success=True, agent_name=self.NAME,
                    output={
                        "url":          url,
                        "turns_played": len(moves_log),
                        "moves_log":    moves_log,
                        "final_summary": final_summary,
                    },
                    elapsed_s=time.time() - t0,
                )

            finally:
                await browser.close()


# ── helpers ───────────────────────────────────────────────────────────────────

def _png_to_data_url(png_bytes: bytes) -> str:
    """Encode raw PNG bytes as a data URL for the /v1/vision endpoint."""
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode()


async def _dismiss_popups(page) -> None:
    """Click through common cookie/consent banners before the game loop starts."""
    # Escape first — closes native browser dialogs and some overlays.
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.4)

    # Ordered list of selectors for common consent/CMP buttons.
    # Funding Choices (Google), OneTrust, Quantcast, custom banners.
    selectors = [
        # Funding Choices (play2048.co uses this)
        ".fc-cta-consent",
        ".fc-button.fc-cta-consent",
        "button.fc-cta-consent",
        # Generic accept/consent patterns
        "button[id*='accept']",
        "button[class*='accept']",
        "[id*='consent'] button",
        "[class*='consent'] button",
        # Text-based (Playwright locator syntax)
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "button:has-text('Agree')",
        "button:has-text('Got it')",
        "button:has-text('OK')",
        "button:has-text('Continue')",
        # OneTrust
        "#onetrust-accept-btn-handler",
        # Quantcast
        ".qc-cmp2-summary-buttons button:first-child",
    ]

    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=600):
                await btn.click()
                print(f"[cua_game] dismissed popup via {sel!r}")
                await asyncio.sleep(0.8)
                break
        except Exception:
            pass

    # Second Escape pass in case clicking opened a nested dialog.
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.3)
