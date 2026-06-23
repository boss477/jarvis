"""ChatGPT, WhatsApp Web, Perplexity research, and Comet app control.

Browser-based tools (ChatGPT, WhatsApp, research_topic) use Friday's own
persistent Chrome profile — log in once in that browser and they stay logged in.
comet_search drives the Comet desktop app directly via keyboard automation.
"""

import subprocess
import time

from .browser import _ensure_page, _run
from ._keyboard import kbd
from pynput.keyboard import Key

_CHATGPT_URL = "https://chatgpt.com/"
_WHATSAPP_URL = "https://web.whatsapp.com/"


def chatgpt_ask(prompt: str) -> str:
    """Open ChatGPT, send `prompt`, wait for the full reply, and return the text."""
    def _do():
        page = _ensure_page()

        if "chatgpt.com" not in page.url:
            page.goto(_CHATGPT_URL, wait_until="domcontentloaded")

        # Detect login wall before trying to type.
        if page.locator('button:has-text("Log in"), a:has-text("Log in")').count():
            return "ChatGPT needs login — open Friday's browser once and sign in."

        box = page.locator("#prompt-textarea")
        box.wait_for(state="visible", timeout=15_000)
        box.click()
        box.fill(prompt)
        page.keyboard.press("Enter")

        # Wait for streaming to start then finish (stop button appears then disappears).
        stop = page.locator('button[aria-label="Stop streaming"]')
        try:
            stop.wait_for(state="visible", timeout=10_000)
            stop.wait_for(state="hidden", timeout=90_000)
        except Exception:
            pass  # may have already finished before we polled

        # Read the last assistant turn.
        msgs = page.locator('[data-message-author-role="assistant"]')
        n = msgs.count()
        if n == 0:
            return "Sent, but couldn't read the response."
        text = msgs.nth(n - 1).inner_text().strip()
        return (text[:1500] + "…") if len(text) > 1500 else text or "Empty response."

    return _run(_do)


def whatsapp_send(contact: str, message: str) -> str:
    """Find `contact` on WhatsApp Web and send them `message`."""
    def _do():
        page = _ensure_page()

        if "web.whatsapp.com" not in page.url:
            page.goto(_WHATSAPP_URL, wait_until="domcontentloaded")

        # Detect QR / login wall.
        if page.locator('canvas[aria-label="Scan me!"], [data-ref]').count():
            return "WhatsApp needs a QR scan — open Friday's browser once and scan the QR code."

        # Open the search box.
        search = page.locator(
            'div[title="Search or start new chat"], '
            '[placeholder="Search or start new chat"]'
        ).first
        search.wait_for(state="visible", timeout=20_000)
        search.click()
        # Clear any previous search before typing.
        search.press("Control+a")
        search.type(contact, delay=50)

        # Wait for results, then click the best match.
        page.wait_for_timeout(1_000)  # brief pause for results to populate
        hit = page.locator(f'span[title="{contact}"]').first
        try:
            hit.wait_for(state="visible", timeout=6_000)
            hit.click()
        except Exception:
            # Fallback: click the first chat-list item that appeared.
            page.locator('div[role="listitem"]').first.click()

        # Type and send the message.
        msg_box = page.locator(
            'div[title="Type a message"], '
            'div[contenteditable="true"][data-tab="10"]'
        ).first
        msg_box.wait_for(state="visible", timeout=8_000)
        msg_box.click()
        msg_box.type(message, delay=30)
        page.keyboard.press("Enter")

        return f"Sent to {contact}: '{message}'"

    return _run(_do)


def comet_search(query: str) -> str:
    """Open the Comet browser app (or focus it if running) and search for `query`."""
    import psutil
    import pywinauto

    # Find a running Comet process.
    comet_pid = None
    for proc in psutil.process_iter(["name", "pid"]):
        if "comet" in proc.info["name"].lower():
            comet_pid = proc.info["pid"]
            break

    if comet_pid is None:
        # Not running — launch it.
        subprocess.Popen(["cmd", "/c", "start", "", "Comet"])
        time.sleep(3)
        # Re-scan for the pid after launch.
        for proc in psutil.process_iter(["name", "pid"]):
            if "comet" in proc.info["name"].lower():
                comet_pid = proc.info["pid"]
                break

    # Bring Comet's main window to the foreground.
    if comet_pid is not None:
        try:
            wins = pywinauto.findwindows.find_windows(process=comet_pid)
            if wins:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(wins[0])
                time.sleep(0.4)
        except Exception:
            pass
    else:
        time.sleep(2)  # launched but pid still not visible — give it a moment

    # Ctrl+L focuses the address / search bar in any Chromium browser.
    kbd.press(Key.ctrl)
    kbd.press("l")
    kbd.release("l")
    kbd.release(Key.ctrl)
    time.sleep(0.3)

    # Select-all to clear any existing URL, then type the query.
    kbd.press(Key.ctrl)
    kbd.press("a")
    kbd.release("a")
    kbd.release(Key.ctrl)
    kbd.type(query)
    kbd.press(Key.enter)
    kbd.release(Key.enter)

    return f"Searching Comet for '{query}'."


def research_topic(query: str) -> str:
    """Research `query` on Perplexity.ai and return the answer text."""
    from urllib.parse import quote_plus

    def _do():
        page = _ensure_page()
        url = f"https://www.perplexity.ai/search?q={quote_plus(query)}"
        page.goto(url, wait_until="domcontentloaded")

        # Wait for the answer to finish streaming.
        # Perplexity shows a "Stop" button while generating; wait for it to vanish.
        stop = page.locator('[aria-label="Stop"], button:has-text("Stop")')
        try:
            stop.first.wait_for(state="visible", timeout=8_000)
            stop.first.wait_for(state="hidden", timeout=60_000)
        except Exception:
            pass  # already done before we checked, or selector changed

        # Grab prose answer text — Perplexity wraps answers in .prose divs.
        answer = page.locator(".prose, [class*='answer'], [class*='Answer']").first
        try:
            answer.wait_for(state="visible", timeout=10_000)
            text = answer.inner_text().strip()
        except Exception:
            # Fallback: grab the whole main content area.
            text = page.locator("main").inner_text().strip()

        text = text[:2000]
        return (text + "…") if len(text) == 2000 else text or "No answer found."

    return _run(_do)
