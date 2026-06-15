"""Controllable browser via Playwright — read/click/fill the web by API, not GUI.

Two design choices, both for speed and correctness:

1. ONE persistent browser, launched lazily and kept alive across tool calls, so
   navigation state survives and we never pay browser-startup cost per command.
2. Playwright's sync objects are bound to the thread that created them, but
   FastMCP runs tools on a thread POOL. So all Playwright work happens on a
   single dedicated worker thread (`_EXEC`); every tool marshals its closure
   there with `_run(...)`. This is the only thread that ever touches `_page`.

Uses the installed Chrome (channel="chrome") — no extra browser download.
"""

from concurrent.futures import ThreadPoolExecutor

# Single owner thread for ALL Playwright calls (see module docstring).
_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="friday-browser")
_pw = None
_browser = None
_page = None


def _run(fn):
    """Run fn() on the browser-owner thread and return its result."""
    return _EXEC.submit(fn).result()


def _alive() -> bool:
    """True only if we hold a live, still-open page (user may close the window)."""
    return (
        _page is not None
        and not _page.is_closed()
        and _browser is not None
        and _browser.is_connected()
    )


def _hard_reset():
    """Drop any dead Playwright handles so the next launch starts clean."""
    global _pw, _browser, _page
    for closer in (lambda: _browser and _browser.close(), lambda: _pw and _pw.stop()):
        try:
            closer()
        except Exception:
            pass
    _pw = _browser = _page = None


def _ensure_page():
    """Return a live page on the owner thread, (re)launching Chrome if needed.

    Recovers when the user closed the window externally — without this, a stale
    _page handle makes goto() fail with 'Target page ... has been closed'."""
    global _pw, _browser, _page
    if _alive():
        return _page
    _hard_reset()  # clear any dead handles from a closed window
    from playwright.sync_api import sync_playwright

    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch(channel="chrome", headless=False)
    _page = _browser.new_page()
    return _page


def browser_open(url: str) -> str:
    """Open `url` in Friday's controllable browser (waits for load)."""
    if "://" not in url:
        url = "https://" + url

    def _do():
        page = _ensure_page()
        page.goto(url, wait_until="domcontentloaded")
        return f"Opened {page.url} — '{page.title()}'."

    return _run(_do)


def browser_read() -> str:
    """Return the visible text of the current page (capped)."""
    def _do():
        if not _alive():
            _hard_reset()
            return "The browser is closed. Use browser_open first."
        text = _page.inner_text("body")
        return text[:3000] + " …(truncated)" if len(text) > 3000 else text

    return _run(_do)


def browser_click(text: str) -> str:
    """Click the first link/button whose visible text contains `text`."""
    def _do():
        if not _alive():
            _hard_reset()
            return "The browser is closed. Use browser_open first."
        loc = _page.get_by_text(text, exact=False).first
        if loc.count() == 0:
            return f"No element with text '{text}'."
        loc.click()
        _page.wait_for_load_state("domcontentloaded")
        return f"Clicked '{text}' — now at '{_page.title()}'."

    return _run(_do)


def browser_fill(label: str, value: str) -> str:
    """Type `value` into the field identified by `label` (placeholder/label/name)."""
    def _do():
        if not _alive():
            _hard_reset()
            return "The browser is closed. Use browser_open first."
        loc = _page.get_by_label(label).or_(_page.get_by_placeholder(label)).first
        if loc.count() == 0:
            return f"No field labelled '{label}'."
        loc.fill(value)
        return f"Filled '{label}'."

    return _run(_do)


def browser_close() -> str:
    """Close Friday's controllable browser and free resources."""
    def _do():
        if _browser is None:
            return "Browser not open."
        _hard_reset()
        return "Browser closed."

    return _run(_do)
