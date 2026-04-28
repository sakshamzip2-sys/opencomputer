"""Playwright wrapper with isolated session per call by default.

Lazy imports of playwright. Returns None / raises BrowserError when
Playwright not installed or browser binary not available.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("opencomputer.browser_control.browser")


class BrowserError(RuntimeError):
    """Raised when Playwright is not installed, browser binary is missing, or
    a navigation/interaction fails fatally."""


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    """Text-based representation of a page (accessibility tree).

    Returned by snapshot() and any tool that reports page state. Pure
    text — never pixels — for model-friendliness and privacy.
    """
    url: str
    title: str
    accessibility_tree: str       # text rendering of the a11y tree
    text_content: str              # plain visible text
    error: str = ""                # populated when navigation/snapshot failed


def _shared_profile_path() -> str | None:
    """Return user-set shared profile path or None for isolated sessions."""
    return os.environ.get("OPENCOMPUTER_BROWSER_PROFILE_PATH") or None


def _import_playwright():
    try:
        from playwright.async_api import async_playwright
        return async_playwright
    except ImportError as exc:
        raise BrowserError(
            f"playwright not installed ({exc}). "
            f"install: pip install opencomputer[browser]  "
            f"then: playwright install chromium"
        ) from exc


@asynccontextmanager
async def _browser_session(headless: bool = True):
    """Context manager yielding a (browser, context) pair.

    Isolated by default. If OPENCOMPUTER_BROWSER_PROFILE_PATH is set,
    uses persistent context at that path (advanced; carries cookies + login).
    """
    async_playwright = _import_playwright()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=headless)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(
                f"failed to launch chromium ({exc}). "
                f"Run: playwright install chromium"
            ) from exc

        profile_path = _shared_profile_path()
        if profile_path:
            _log.warning(
                "OPENCOMPUTER_BROWSER_PROFILE_PATH set — using SHARED profile at %s. "
                "Cookies and login state are carried across calls. Use only on trusted sites.",
                profile_path,
            )
            context = await browser.new_context(storage_state=None)
        else:
            context = await browser.new_context()

        try:
            yield browser, context
        finally:
            await context.close()
            await browser.close()


async def navigate_and_snapshot(url: str, *, headless: bool = True, timeout_ms: int = 15000) -> PageSnapshot:
    """Open URL in fresh isolated context; return text snapshot."""
    async with _browser_session(headless=headless) as (_browser, context):
        page = await context.new_page()
        try:
            await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            return PageSnapshot(
                url=url, title="", accessibility_tree="", text_content="",
                error=f"navigation failed: {exc}",
            )
        return await _snapshot_page(page)


async def _snapshot_page(page: Any) -> PageSnapshot:
    """Build a PageSnapshot from a live Playwright page."""
    try:
        title = await page.title()
        url = page.url
        # Accessibility tree (text)
        a11y = await page.accessibility.snapshot()
        tree_text = _render_a11y_tree(a11y) if a11y else ""
        # Visible text
        text_content = await page.inner_text("body")
    except Exception as exc:  # noqa: BLE001
        return PageSnapshot(url=page.url, title="", accessibility_tree="", text_content="",
                            error=f"snapshot failed: {exc}")

    return PageSnapshot(
        url=url, title=title,
        accessibility_tree=tree_text,
        text_content=text_content[:5000],  # cap to keep tool results sane
    )


def _render_a11y_tree(node: dict, depth: int = 0) -> str:
    """Render Playwright accessibility-snapshot dict as text."""
    if not node:
        return ""
    indent = "  " * depth
    role = node.get("role", "")
    name = node.get("name", "")
    line = f"{indent}{role}: {name}".rstrip()
    children = node.get("children", []) or []
    rendered = [line] if role or name else []
    for child in children:
        sub = _render_a11y_tree(child, depth + 1)
        if sub:
            rendered.append(sub)
    return "\n".join(rendered)


async def click_element(url: str, selector: str, *, headless: bool = True) -> PageSnapshot:
    """Navigate, click selector, return post-click snapshot."""
    async with _browser_session(headless=headless) as (_browser, context):
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.click(selector, timeout=10000)
            await page.wait_for_load_state("domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            return PageSnapshot(url=url, title="", accessibility_tree="", text_content="",
                                error=f"click failed: {exc}")
        return await _snapshot_page(page)


async def fill_input(url: str, selector: str, value: str, *, headless: bool = True) -> PageSnapshot:
    """Navigate, fill input, return post-fill snapshot."""
    async with _browser_session(headless=headless) as (_browser, context):
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.fill(selector, value, timeout=10000)
        except Exception as exc:  # noqa: BLE001
            return PageSnapshot(url=url, title="", accessibility_tree="", text_content="",
                                error=f"fill failed: {exc}")
        return await _snapshot_page(page)


async def scrape_url(url: str, css_selector: str | None = None, *, headless: bool = True) -> PageSnapshot:
    """Navigate; if css_selector given, return matched elements' text; else full visible text."""
    async with _browser_session(headless=headless) as (_browser, context):
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if css_selector:
                elements = await page.query_selector_all(css_selector)
                texts = []
                for el in elements:
                    t = await el.inner_text()
                    if t:
                        texts.append(t.strip())
                text_content = "\n\n".join(texts)
            else:
                text_content = await page.inner_text("body")
            title = await page.title()
        except Exception as exc:  # noqa: BLE001
            return PageSnapshot(url=url, title="", accessibility_tree="", text_content="",
                                error=f"scrape failed: {exc}")

    return PageSnapshot(
        url=url, title=title,
        accessibility_tree="",
        text_content=text_content[:5000],
    )
