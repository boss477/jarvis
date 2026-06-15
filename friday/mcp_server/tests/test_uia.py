"""uia tests: monkeypatch the foreground window with fake UIA elements so we
never touch a live desktop tree."""

from friday.mcp_server import uia


class FakeEl:
    def __init__(self, text, *, invokable=True, raises_text=False):
        self._text = text
        self._invokable = invokable
        self._raises_text = raises_text
        self.invoked = False
        self.clicked = False

    def window_text(self):
        if self._raises_text:
            raise RuntimeError("no text pattern")
        return self._text

    def invoke(self):
        if not self._invokable:
            raise RuntimeError("Invoke not supported")
        self.invoked = True

    def click_input(self):
        self.clicked = True


class FakeWin:
    def __init__(self, els):
        self._els = els

    def descendants(self):
        return self._els


def _patch(monkeypatch, els):
    monkeypatch.setattr(uia, "_foreground", lambda: FakeWin(els))


def test_click_invokes_matching_element(monkeypatch):
    btn = FakeEl("Save")
    _patch(monkeypatch, [FakeEl("Cancel"), btn])
    msg = uia.click_element("save")  # case-insensitive substring
    assert btn.invoked and not btn.clicked
    assert "Save" in msg


def test_click_falls_back_to_real_click(monkeypatch):
    btn = FakeEl("Submit", invokable=False)
    _patch(monkeypatch, [btn])
    uia.click_element("Submit")
    assert btn.clicked and not btn.invoked


def test_click_no_match(monkeypatch):
    _patch(monkeypatch, [FakeEl("OK")])
    assert "No element named" in uia.click_element("Delete")


def test_click_empty_name():
    assert "empty" in uia.click_element("   ")


def test_click_skips_text_errors(monkeypatch):
    good = FakeEl("Apply")
    _patch(monkeypatch, [FakeEl("x", raises_text=True), good])
    uia.click_element("apply")
    assert good.invoked


def test_list_elements_dedupes_and_caps(monkeypatch):
    els = [FakeEl("Dup"), FakeEl("Dup")] + [FakeEl(f"b{i}") for i in range(100)]
    _patch(monkeypatch, els)
    lines = uia.list_elements().splitlines()
    assert lines.count("Dup") == 1
    assert len(lines) == uia._MAX_LIST


def test_list_elements_empty(monkeypatch):
    _patch(monkeypatch, [])
    assert "No named elements" in uia.list_elements()
