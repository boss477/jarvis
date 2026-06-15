"""browser tests: run closures inline (no real owner thread) against a fake
Playwright page, so nothing launches Chrome."""

import pytest

from friday.mcp_server import browser


@pytest.fixture(autouse=True)
def inline_run(monkeypatch):
    # _run normally marshals to the owner thread; run inline for tests.
    monkeypatch.setattr(browser, "_run", lambda fn: fn())
    yield
    browser._page = None


class FakeLocator:
    def __init__(self, count=1):
        self._count = count
        self.clicked = False
        self.filled = None
    @property
    def first(self):
        return self
    def count(self):
        return self._count
    def click(self):
        self.clicked = True
    def fill(self, v):
        self.filled = v
    def or_(self, other):
        return self if self._count else other


class FakePage:
    def __init__(self):
        self.url = "https://example.com/"
        self.goto_url = None
        self._text_loc = FakeLocator()
        self._label_loc = FakeLocator()
        self._placeholder_loc = FakeLocator(0)
    def goto(self, url, wait_until=None):
        self.goto_url = url
        self.url = url
    def title(self):
        return "Example"
    def inner_text(self, sel):
        return "hello world"
    def get_by_text(self, text, exact=False):
        return self._text_loc
    def get_by_label(self, label):
        return self._label_loc
    def get_by_placeholder(self, label):
        return self._placeholder_loc
    def wait_for_load_state(self, state):
        pass


def _set_page(monkeypatch, page):
    monkeypatch.setattr(browser, "_ensure_page", lambda: page)
    browser._page = page


def test_open_adds_scheme(monkeypatch):
    page = FakePage()
    monkeypatch.setattr(browser, "_ensure_page", lambda: page)
    msg = browser.browser_open("example.com")
    assert page.goto_url == "https://example.com"
    assert "Example" in msg


def test_read_requires_open():
    browser._page = None
    assert "No page open" in browser.browser_read()


def test_read_returns_text(monkeypatch):
    _set_page(monkeypatch, FakePage())
    assert "hello world" in browser.browser_read()


def test_click_found(monkeypatch):
    page = FakePage()
    _set_page(monkeypatch, page)
    msg = browser.browser_click("Login")
    assert page._text_loc.clicked
    assert "Clicked" in msg


def test_click_not_found(monkeypatch):
    page = FakePage()
    page._text_loc = FakeLocator(0)
    _set_page(monkeypatch, page)
    assert "No element" in browser.browser_click("Nope")


def test_fill_uses_label(monkeypatch):
    page = FakePage()
    _set_page(monkeypatch, page)
    msg = browser.browser_fill("Email", "a@b.com")
    assert page._label_loc.filled == "a@b.com"
    assert "Filled" in msg


def test_close_when_not_open():
    browser._browser = None
    assert "not open" in browser.browser_close()
