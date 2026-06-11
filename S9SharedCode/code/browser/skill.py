"""Session 9: the Browser skill — cascade wrapper around the layered drivers.

The wrapper translates the orchestrator's NodeSpec contract into the
typed BrowserOutput / AgentResult contract, and owns the layer cascade:

    Layer 1  — HTML extract via trafilatura (no LLM)
    Layer 2a — deterministic selectors (only if metadata.selectors is given)
    Layer 2b — A11yDriver        (text-only, V9 /v1/chat)
    Layer 3  — SetOfMarksDriver  (vision, V9 /v1/vision)

Escalation rule: a layer escalates when its output is empty or evidently
insufficient. The skill stops at the first layer that produces a useful
answer.

Gateway-access is a first-class failure: if Layer 1's fetch returns a
known CAPTCHA / login-wall / hCaptcha marker, the skill returns
immediately with error_code="gateway_blocked" and does not attempt
the later layers. The orchestrator's recovery path picks this up via
the failure_report (it contains the literal token "gateway_blocked")
and re-invokes the Planner.

This file is the ONLY new code in the integration. The four files
already on disk (client.py, dom.py, highlight.py, driver.py) are
ported verbatim from S9SharedCode/code/browser/ and untouched.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import trafilatura
from playwright.async_api import async_playwright

from schemas import AgentResult, BrowserOutput, NodeSpec

from .client import V9Client
from .driver import A11yDriver, DriverConfig, DriverResult, SetOfMarksDriver
import pdb


# ── gateway-block detection ──────────────────────────────────────────────────
# Kept here (next to the cascade that uses it) rather than mutating the
# ported dom.py.  Short, obvious patterns — when this list grows past a
# screenful we should consolidate, but for now explicit is better.
_GATEWAY_BLOCK_MARKERS = (
    # Generic CAPTCHA / hCaptcha / reCAPTCHA. Needles MUST be specific
    # enough that an article ABOUT captchas does not false-positive — we
    # match on class/attribute strings the widgets emit, not their names.
    ("captcha",                "Let's confirm you are human"),
    ("captcha",                "Enter the characters you see below"),
    ("captcha",                "Robot Check"),
    ("captcha",                "Please verify you are a human"),
    ("captcha",                "/errors/validateCaptcha"),
    ("hcaptcha",               'class="h-captcha"'),
    ("hcaptcha",               "data-hcaptcha-widget-id"),
    ("recaptcha",              'class="g-recaptcha"'),
    ("recaptcha",              "g-recaptcha-response"),
    # Cloudflare interstitials.
    ("cloudflare",             "Checking your browser before accessing"),
    ("cloudflare",             "cf-browser-verification"),
    ("cloudflare",             "cf-challenge-running"),
    # Login walls.  Conservative — only the literal sign-in-required pages.
    ("login_wall",             "You must be logged in"),
    ("login_wall",             "Sign in to continue"),
    ("login_wall",             "Please log in to continue"),
)


def detect_gateway_block(html: str) -> str | None:
    """Return the block type when `html` looks like a gateway-access page
    (CAPTCHA / Cloudflare / login wall), else None. Conservative — false
    positives would mis-route real content to recovery."""
    if not html:
        return None
    h = html.lower()
    for kind, needle in _GATEWAY_BLOCK_MARKERS:
        if needle.lower() in h:
            return kind
    return None


# ── Layer 1: pure-HTTP extraction ────────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (compatible; S9-Browser-Skill/0.1; +llm_gatewayV9)"
)


async def _fetch_html(url: str, timeout: float = 30.0) -> tuple[str, str]:
    """Returns (html, final_url)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.text, str(r.url)


def _extract(html: str) -> str:
    text = trafilatura.extract(
        html, include_links=True, include_formatting=False, favor_recall=True,
    )
    return (text or "").strip()


def _is_useful_extract(content: str, goal: str) -> bool:
    """Coarse usefulness check. We trust the gateway/recovery to catch
    genuine no-content failures; this gate only filters obvious nothing
    (< ~200 chars) or the case where the page rendered but the goal asks
    for an interaction (`click`, `fill`, `select`, etc.) — for those goals
    extraction is never sufficient regardless of content length."""
    if len(content) < 200:
        return False
    interactive_verbs = ("click", "fill", "select", "type", "drag",
                         "filter", "sort", "submit", "navigate")
    if any(v in goal.lower() for v in interactive_verbs):
        return False
    return True


# ── the skill ────────────────────────────────────────────────────────────────
class BrowserSkill:
    NAME = "browser"

    def __init__(self, *, gateway_url: str = "http://localhost:8109",
                 agent_tag: str = "browser",
                 a11y_provider_pin: str | None = "gemini",
                 vision_provider_pin: str | None = None,
                 artifacts_root: str | None = None,
                 max_steps_a11y: int = 12,
                 max_steps_vision: int = 12,
                 wall_clock_s: float = 90.0,
                 max_detail_pages: int = 3,
                 session: str | None = None):
        self.gateway_url = gateway_url
        self.agent_tag = agent_tag
        self.a11y_provider_pin = a11y_provider_pin
        self.vision_provider_pin = vision_provider_pin
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.max_steps_a11y = max_steps_a11y
        self.max_steps_vision = max_steps_vision
        self.max_detail_pages = max_detail_pages
        self.wall_clock_s = wall_clock_s
        # Forwarded to V9 so the gateway ledger can attribute each call to
        # the orchestrator session that drove it.
        self.session = session

    # ── public entry point ─────────────────────────────────────────────────
    async def run(self, node: NodeSpec) -> AgentResult:
        url = node.metadata.get("url") or (node.inputs[0] if node.inputs else "")
        goal = node.metadata.get("goal") or "extract main content"
        force_path = node.metadata.get("force_path")
        print(f"[browser] ── start ──────────────────────────────────────")
        print(f"[browser] url       : {url}")
        print(f"[browser] goal      : {goal}")
        print(f"[browser] force_path: {force_path}")
        if not url:
            print(f"[browser] ✗ no url given — returning error immediately")
            return self._pack_error("", goal, "interaction_failed",
                                    "no url given (metadata.url or inputs[0])")
        t0 = time.time()
        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag,
                          session=self.session)
        artifacts_dir = (
            str(self.artifacts_root / f"browser_{int(t0)}")
            if self.artifacts_root else None
        )

        # ── Layer 1: extract ────────────────────────────────────────────────
        print(f"[browser] layer1: fetching HTML via httpx ...")
        layer1_http_error: str | None = None
        try:
            html, final_url = await _fetch_html(url)
            print(f"[browser] layer1: fetch ok — {len(html)} chars, final_url={final_url}")
        except httpx.HTTPError as e:
            layer1_http_error = f"layer1 fetch failed: {e}"
            html, final_url = "", url
            print(f"[browser] layer1: fetch FAILED — {layer1_http_error}")

        if html:
            block = detect_gateway_block(html)
            if block:
                print(f"[browser] layer1: gateway block detected → '{block}' — returning blocked")
                return self._pack(url, goal, "blocked", turns=0,
                                  content=f"blocked: {block} on {final_url}",
                                  final_url=final_url, elapsed=time.time() - t0)
            content = _extract(html)
            useful = _is_useful_extract(content, goal)
            print(f"[browser] layer1: extract → {len(content)} chars, is_useful={useful}")
            if useful:
                print(f"[browser] layer1: ✓ extract sufficient — returning extract")
                return self._pack(url, goal, "extract", turns=0,
                                  content=content, final_url=final_url,
                                  elapsed=time.time() - t0)
            print(f"[browser] layer1: extract insufficient — escalating")
        else:
            print(f"[browser] layer1: no HTML — skipping extract/block-check, escalating")

        # ── Layer 2a: deterministic selectors (only if caller gave any) ────
        selectors = node.metadata.get("selectors") or []
        if selectors:
            print(f"[browser] layer2a: trying {len(selectors)} deterministic selector(s) ...")
            det = await self._try_deterministic(url, goal, selectors)
            if det is not None:
                print(f"[browser] layer2a: deterministic result success={det.success}")
                return det if det.success else self._pack_error(
                    url, goal, "interaction_failed",
                    det.error or "deterministic path failed",
                    elapsed=time.time() - t0,
                )
            print(f"[browser] layer2a: deterministic returned None — escalating to a11y")
        else:
            print(f"[browser] layer2a: no selectors in metadata — skipping")

        # ── Layer 2b: a11y ──────────────────────────────────────────────────
        if force_path == "vision":
            print(f"[browser] layer2b: skipped (force_path=vision)")
            a11y_result = DriverResult(success=False, note="skipped by force_path=vision")
        else:
            print(f"[browser] layer2b: running A11yDriver ...")
            a11y_result = await self._drive(
                A11yDriver, url, goal, client, artifacts_dir,
                self.a11y_provider_pin, self.max_steps_a11y,
                max_detail_pages=self.max_detail_pages,
            )
            print(f"[browser] layer2b: a11y done — success={a11y_result.success}  "
                  f"gateway_blocked={getattr(a11y_result,'gateway_blocked',False)}  "
                  f"note={a11y_result.note!r}")
        if getattr(a11y_result, "gateway_blocked", False):
            print(f"[browser] layer2b: gateway block after JS render — returning blocked")
            return self._pack(url, goal, "blocked", turns=0,
                              content=f"blocked: {a11y_result.note or 'gateway blocked after JS render'}",
                              elapsed=time.time() - t0)
        if a11y_result.success:
            print(f"[browser] layer2b: ✓ a11y succeeded — returning a11y")
            return self._pack_driver("a11y", url, goal, a11y_result,
                                     final_url=a11y_result.final_url,
                                     elapsed=time.time() - t0)
        print(f"[browser] layer2b: a11y failed — escalating to vision")

        # ── Layer 3: vision ─────────────────────────────────────────────────
        print(f"[browser] layer3: running SetOfMarksDriver ...")
        vis_result = await self._drive(
            SetOfMarksDriver, url, goal, client, artifacts_dir,
            self.vision_provider_pin, self.max_steps_vision,
            max_detail_pages=self.max_detail_pages,
        )
        print(f"[browser] layer3: vision done — success={vis_result.success}  "
              f"gateway_blocked={getattr(vis_result,'gateway_blocked',False)}  "
              f"note={vis_result.note!r}")
        if getattr(vis_result, "gateway_blocked", False):
            print(f"[browser] layer3: gateway block after JS render — returning blocked")
            return self._pack(url, goal, "blocked", turns=0,
                              content=f"blocked: {vis_result.note or 'gateway blocked after JS render'}",
                              elapsed=time.time() - t0)
        if vis_result.success:
            print(f"[browser] layer3: ✓ vision succeeded — returning vision")
            return self._pack_driver("vision", url, goal, vis_result,
                                     final_url=vis_result.final_url,
                                     elapsed=time.time() - t0)

        last_err = (vis_result.note or a11y_result.note
                    or layer1_http_error or "all layers exhausted")
        print(f"[browser] layer3: vision failed — all layers exhausted")
        print(f"[browser] ✗ returning blocked — last_err: {last_err}")
        return self._pack(url, goal, "blocked", turns=0,
                          content=f"blocked: all layers exhausted; last: {last_err}",
                          final_url=final_url, elapsed=time.time() - t0)

    # ── per-layer driver runs ──────────────────────────────────────────────
    async def _drive(self, DriverCls, url, goal, client, artifacts_dir,
                     provider_pin, max_steps, max_detail_pages: int = 0):
        # Place each layer's per-turn artifacts under its own subdir so
        # turn_##_* filenames from one layer don't overwrite another's.
        if artifacts_dir:
            from pathlib import Path as _P
            sub = _P(artifacts_dir) / DriverCls.LAYER_NAME
            sub.mkdir(parents=True, exist_ok=True)
            artifacts_dir = str(sub)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',"
                "{get:()=>undefined});"
            )
            page = await ctx.new_page()
            try:
                print(f"[browser._drive] {DriverCls.__name__}: navigating to {url} ...")
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                print(f"[browser._drive] {DriverCls.__name__}: page loaded — {page.url}")
                rendered_html = await page.content()
                print(f"[browser._drive] {DriverCls.__name__}: rendered HTML = {len(rendered_html)} chars")
                kind = detect_gateway_block(rendered_html)
                print(f"[browser._drive] {DriverCls.__name__}: post-JS gateway block check → {kind!r}")
                if kind:
                    await browser.close()
                    out = DriverResult(
                        success=False,
                        note=f"gateway_blocked ({kind}) detected after JS render at {page.url}",
                    )
                    out.gateway_blocked = True
                    return out
                await asyncio.sleep(1.0)
                cfg = DriverConfig(
                    goal=goal, max_steps=max_steps, max_failures=3,
                    artifacts_dir=artifacts_dir, provider=provider_pin,
                )
                drv = DriverCls(page, client, cfg)
                # Augment the result with final_url + extracted text so
                # _pack_driver can fill BrowserOutput uniformly.
                result = await drv.run()
                print(f"[browser._drive] {DriverCls.__name__}: driver.run() done — "
                      f"success={result.success}  note={result.note!r}")
                result.final_url = page.url
                result.extracted = ""
                try:
                    page_html = await page.content()
                    traf = _extract(page_html)
                    if len(traf) >= 500:
                        result.extracted = traf
                        print(f"[browser._drive] {DriverCls.__name__}: extraction=trafilatura  "
                              f"{len(result.extracted)} chars")
                    else:
                        # Trafilatura stripped too much — common for React/SPA apps
                        # like HuggingFace where model cards are not static HTML.
                        # inner_text gives every visible text node on the rendered page.
                        inner = await page.inner_text("body")
                        result.extracted = inner[:15000]
                        print(f"[browser._drive] {DriverCls.__name__}: extraction=inner_text "
                              f"(trafilatura only {len(traf)} chars)  "
                              f"{len(result.extracted)} chars")
                except Exception as _e:                    # noqa: BLE001
                    print(f"[browser._drive] {DriverCls.__name__}: extraction failed — {_e}")
                result.turns = len(drv.steps)
                result.actions = [
                    {"turn": s.turn, "actions": s.actions, "outcome": s.outcome}
                    for s in drv.steps
                ]
                # After the driver navigates (and filters/sorts) a listing page,
                # open the top N item detail pages so downstream skills receive
                # real per-item data rather than the thin listing-page snapshot.
                # This generalises to any listing: model directories, product
                # grids, search results, leaderboards, etc. — if no depth-2
                # same-domain links are found (e.g. single-page lookup) the
                # method returns an empty string and nothing changes.
                if result.success and max_detail_pages > 0:
                    detail_text = await self._follow_detail_pages(
                        page, max_detail_pages
                    )
                    if detail_text:
                        result.extracted = result.extracted + "\n\n" + detail_text
                        print(f"[browser._drive] {DriverCls.__name__}: "
                              f"appended {len(detail_text)} chars from detail pages — "
                              f"total extracted now {len(result.extracted)} chars")
                return result
            finally:
                await browser.close()

    # First path-segment words that reliably indicate navigation / feature
    # pages rather than item-detail pages.  Kept small and general — extend
    # only when a new site type proves necessary.
    _NAV_PREFIXES: frozenset[str] = frozenset({
        "join", "login", "signup", "signin", "register", "logout",
        "about", "contact", "careers", "press", "legal", "privacy", "terms",
        "docs", "documentation", "help", "support", "faq",
        "blog", "news", "updates", "changelog",
        "pricing", "enterprise", "pro", "business",
        "api", "developers", "developer",
        "status", "community", "discord", "forum", "forums",
        # HuggingFace top-level feature paths that are NOT model cards
        "inference", "spaces", "datasets", "collections",
        "tasks", "tags", "papers", "leaderboards",
    })

    async def _follow_detail_pages(self, page, max_pages: int) -> str:
        """Visit the top N item-detail links on the current listing page and
        return their extracted text concatenated.

        Two-tier link selection (generalises to model directories, product
        grids, search results, leaderboards, GitHub trending, etc.):

          Tier 1 — scope to <main> / [role=main]:
            Most modern sites place the result grid inside a semantic main
            element, keeping navbar and footer links out of scope.

          Tier 2 — filter by path structure:
            - same domain, path depth >= 2
            - first path segment NOT in _NAV_PREFIXES (excludes feature/nav
              paths like /join/discord or /inference/models)
            - non-trivial link text (> 3 chars)
            - links appear in DOM order = visual ranking order on listing pages

        Content is capped at 5 000 chars per page to stay within context limits.
        """
        from urllib.parse import urlparse

        current_url = page.url
        base_domain = urlparse(current_url).netloc

        # Tier 1: scope to semantic main content — avoids nav/footer/sidebar
        try:
            link_data = await page.eval_on_selector_all(
                "main a[href], [role='main'] a[href]",
                "els => els.map(e => ({href: e.href, text: e.innerText.trim().slice(0,120)}))",
            )
            if len(link_data) < 3:
                # No semantic main or too few links — fall back to full body
                print(f"[browser] _follow_detail_pages: main has {len(link_data)} link(s), "
                      f"falling back to body")
                link_data = await page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => ({href: e.href, text: e.innerText.trim().slice(0,120)}))",
                )
        except Exception as _e:
            print(f"[browser] _follow_detail_pages: link enumeration failed — {_e}")
            return ""

        # Tier 2: filter to content-like depth-2 same-domain links
        seen: set[str] = set()
        candidates: list[str] = []
        for item in link_data:
            href = (item.get("href") or "").strip()
            text = (item.get("text") or "").strip()
            if not href or len(text) < 3:
                continue
            parsed     = urlparse(href)
            path_parts = [p for p in parsed.path.strip("/").split("/") if p]
            first_seg  = path_parts[0].lower() if path_parts else ""
            if (parsed.netloc == base_domain
                    and len(path_parts) >= 2
                    and first_seg not in self._NAV_PREFIXES
                    and href not in seen
                    and href != current_url):
                seen.add(href)
                candidates.append(href)
            if len(candidates) >= max_pages:
                break

        if not candidates:
            print(f"[browser] _follow_detail_pages: no content links found — skipping")
            return ""

        print(f"[browser] _follow_detail_pages: visiting {len(candidates)} detail page(s): "
              f"{candidates}")

        parts: list[str] = []
        for href in candidates:
            try:
                await page.goto(href, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(0.5)
                html    = await page.content()
                content = _extract(html)
                if len(content) < 300:
                    content = (await page.inner_text("body"))[:5000]
                else:
                    content = content[:5000]
                if content.strip():
                    parts.append(f"--- {page.url} ---\n{content.strip()}")
                    print(f"[browser] _follow_detail_pages: {page.url} → {len(content)} chars")
            except Exception as _e:
                print(f"[browser] _follow_detail_pages: {href} failed — {_e}")

        return "\n\n".join(parts)

    async def _try_deterministic(self, url, goal, selectors) -> AgentResult | None:
        """Runs caller-supplied selector instructions through Playwright. Each
        step is `{action, selector, value?}`. Returns AgentResult on success
        or None to let the cascade fall through to a11y."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 900},
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                for i, step in enumerate(selectors, start=1):
                    sel = step.get("selector")
                    if not sel:
                        await browser.close()
                        return None
                    loc = page.locator(sel).first
                    try:
                        await loc.wait_for(state="visible", timeout=8000)
                    except Exception:                          # noqa: BLE001
                        await browser.close()
                        return None
                    if step.get("action") == "fill":
                        await loc.fill(step.get("value", ""))
                    elif step.get("action") == "click":
                        await loc.click()
                    elif step.get("action") == "key":
                        await page.keyboard.press(step.get("value", "Enter"))
                content = _extract(await page.content())
                final = page.url
                await browser.close()
                return self._pack(
                    url, goal, "deterministic", turns=len(selectors),
                    content=content, final_url=final, elapsed=0.0,
                )
            except Exception:                          # noqa: BLE001
                await browser.close()
                return None

    # ── packers ────────────────────────────────────────────────────────────
    def _pack(self, url, goal, path, *, turns, content=None, actions=None,
              final_url=None, elapsed=0.0) -> AgentResult:
        out = BrowserOutput(
            url=url, goal=goal, path=path, turns=turns,
            content=content, actions=actions or [], final_url=final_url,
        )
        return AgentResult(
            success=True, agent_name=self.NAME,
            output=out.model_dump(), elapsed_s=elapsed,
        )

    def _pack_driver(self, path, url, goal, drv_result,
                     *, final_url, elapsed) -> AgentResult:
        extracted = getattr(drv_result, "extracted", None) or ""
        note      = getattr(drv_result, "note", "") or ""
        # Always lead with the source URL so the downstream distiller includes
        # it in its output and the critic can verify the data is sourced rather
        # than fabricated from parametric memory.
        source_line = f"Source URL: {final_url or url}"
        parts = [source_line]
        if note:
            parts.append(f"Agent summary: {note}")
        if extracted:
            parts.append(extracted)
        content = "\n\n".join(parts)
        out = BrowserOutput(
            url=url, goal=goal, path=path,
            turns=getattr(drv_result, "turns", 0) or 0,
            content=content or None,
            actions=getattr(drv_result, "actions", []) or [],
            final_url=final_url,
        )
        return AgentResult(
            success=True, agent_name=self.NAME,
            output=out.model_dump(), elapsed_s=elapsed,
        )

    def _pack_error(self, url, goal, code, msg, *, elapsed=0.0) -> AgentResult:
        out = BrowserOutput(
            url=url or "", goal=goal, path="extract", turns=0, content=None,
        )
        return AgentResult(
            success=False, agent_name=self.NAME,
            output=out.model_dump(), error=msg, error_code=code,
            elapsed_s=elapsed,
        )
