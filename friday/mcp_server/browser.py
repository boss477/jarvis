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

import os
import shutil
from concurrent.futures import ThreadPoolExecutor

# Friday drives its OWN dedicated Chrome profile dir, separate from the user's
# everyday Chrome. Reason: Chrome locks a profile dir to one process, so sharing
# the real Fawaz profile fails whenever normal Chrome is open. This dir is seeded
# once (see _seed_profile) from the real profile so Friday starts logged in.
_USER_DATA = os.getenv(
    "FRIDAY_CHROME_USER_DATA",
    os.path.expandvars(r"%LOCALAPPDATA%\Friday\ChromeProfile"),
)
_PROFILE = os.getenv("FRIDAY_CHROME_PROFILE", "Default")

# Source profile to copy logins from, on first run only.
_SEED_USER_DATA = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
_SEED_PROFILE = os.getenv("FRIDAY_CHROME_SEED_PROFILE", "Default")

# Bulky/regenerable dirs not worth copying when seeding.
_SEED_SKIP = (
    "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache",
    "GrShaderCache", "ShaderCache", "Service Worker", "Application Cache",
)


def _seed_profile():
    """One-time: copy the user's real Chrome profile into Friday's own dir so it
    starts logged in. Best-effort — locked/missing files are skipped, and any
    logins that don't survive the copy can simply be re-done once in Friday's
    browser (they then persist)."""
    if os.path.exists(_USER_DATA):
        return  # already seeded (or user-provided)
    src_profile = os.path.join(_SEED_USER_DATA, _SEED_PROFILE)
    if not os.path.isdir(src_profile):
        return  # nothing to seed from; start fresh
    os.makedirs(_USER_DATA, exist_ok=True)

    def _safe_copy(src, dst):
        try:
            shutil.copy2(src, dst)
        except OSError:
            pass  # locked (Chrome open) or unreadable — skip, login may re-prompt

    # Local State holds the cookie-encryption key; cookies can't decrypt without it.
    src_ls = os.path.join(_SEED_USER_DATA, "Local State")
    if os.path.isfile(src_ls):
        _safe_copy(src_ls, os.path.join(_USER_DATA, "Local State"))

    shutil.copytree(
        src_profile,
        os.path.join(_USER_DATA, _PROFILE),
        ignore=shutil.ignore_patterns(*_SEED_SKIP),
        copy_function=_safe_copy,
        dirs_exist_ok=True,
    )

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
    # A persistent context has no is_connected(); treat presence as alive and
    # rely on _page.is_closed() to detect a window the user closed.
    is_connected = getattr(_browser, "is_connected", lambda: True)
    return (
        _page is not None
        and not _page.is_closed()
        and _browser is not None
        and is_connected()
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

    _seed_profile()  # first run only: copy logins from the real Chrome profile
    _pw = sync_playwright().start()
    # Persistent context = launch into Friday's own user-data dir + profile, so
    # Chrome opens logged in without locking the user's everyday profile.
    # Returns a context, not a Browser.
    _browser = _pw.chromium.launch_persistent_context(
        _USER_DATA,
        channel="chrome",
        headless=False,
        args=[f"--profile-directory={_PROFILE}"],
    )
    _page = _browser.pages[0] if _browser.pages else _browser.new_page()
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


def browser_fill(label: str, value: str, submit: bool = False) -> str:
    """Type `value` into the field identified by `label` (placeholder/label/name).

    If `submit` is True, press Enter afterwards (sends chat messages, search
    boxes, login forms) and wait for any resulting navigation."""
    def _do():
        if not _alive():
            _hard_reset()
            return "The browser is closed. Use browser_open first."
        loc = _page.get_by_label(label).or_(_page.get_by_placeholder(label)).first
        if loc.count() == 0:
            return f"No field labelled '{label}'."
        loc.fill(value)
        if submit:
            loc.press("Enter")
            _page.wait_for_load_state("domcontentloaded")
            return f"Filled '{label}' and submitted."
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
