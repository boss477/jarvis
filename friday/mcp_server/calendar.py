"""Google Calendar — add events via the eventedit URL trick.

No extra OAuth needed: Friday's persistent Chrome profile is already logged
into Google. The LLM should pass `when` as a natural-language string like
"Friday 3pm", "tomorrow at 14:00", or "2026-06-20 15:00"; dateutil parses it.
"""

from datetime import timedelta
from urllib.parse import quote_plus

from .browser import _ensure_page, _run


def calendar_add(title: str, when: str, duration_minutes: int = 60) -> str:
    """Add a Google Calendar event titled `title` starting at `when`.

    `when` is a natural-language date/time string, e.g. "Friday 3pm" or
    "tomorrow at 2pm". `duration_minutes` defaults to 60."""
    from dateutil import parser as dtparser
    from datetime import datetime

    # Parse with today as the default date so "Friday 3pm" resolves correctly.
    now = datetime.now()
    try:
        start = dtparser.parse(when, default=now.replace(hour=9, minute=0, second=0, microsecond=0))
    except Exception as e:
        return f"Couldn't parse '{when}' as a date/time: {e}"

    # If the parsed time is in the past (e.g. "Friday" already passed this week),
    # bump it forward by 7 days so the intent is still met.
    if start < now:
        start += timedelta(days=7)

    end = start + timedelta(minutes=duration_minutes)

    # Google Calendar eventedit URL — pre-fills title and time, no clicking needed.
    fmt = "%Y%m%dT%H%M%S"
    dates = f"{start.strftime(fmt)}/{end.strftime(fmt)}"
    url = (
        "https://calendar.google.com/calendar/r/eventedit"
        f"?text={quote_plus(title)}&dates={dates}&sf=true"
    )

    def _do():
        page = _ensure_page()
        page.goto(url, wait_until="domcontentloaded")

        # Detect login wall.
        if page.locator('a:has-text("Sign in"), [data-authuser]').count() == 0:
            pass  # logged in — continue
        if "accounts.google.com" in page.url:
            return "Google Calendar needs login — open Friday's browser once and sign in."

        # Click Save (aria-label varies slightly by locale).
        save = page.locator(
            'button[aria-label="Save"], '
            'button:has-text("Save"), '
            'div[data-view="save"]'
        ).first
        try:
            save.wait_for(state="visible", timeout=12_000)
            save.click()
            page.wait_for_load_state("domcontentloaded")
            return (
                f"Added '{title}' on "
                f"{start.strftime('%A %b %d at %I:%M %p').lstrip('0')}."
            )
        except Exception:
            # The form loaded but Save didn't click — leave it open for the user.
            return (
                f"Google Calendar is open for '{title}' on "
                f"{start.strftime('%A %b %d at %I:%M %p')} — click Save to confirm."
            )

    return _run(_do)
