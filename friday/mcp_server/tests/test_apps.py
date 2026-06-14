from friday.mcp_server import apps


def test_open_app_maps_friendly_name(monkeypatch):
    calls = {}
    monkeypatch.setattr(apps.subprocess, "Popen", lambda args, **kw: calls.setdefault("args", args))
    msg = apps.open_app("vs code")
    assert "code" in calls["args"]      # mapped from "vs code"
    assert "Opening" in msg


def test_open_app_unknown_name_passes_through(monkeypatch):
    calls = {}
    monkeypatch.setattr(apps.subprocess, "Popen", lambda args, **kw: calls.setdefault("args", args))
    apps.open_app("someapp")
    assert "someapp" in calls["args"]


def test_open_url_adds_scheme(monkeypatch):
    opened = {}
    monkeypatch.setattr(apps.webbrowser, "open", lambda url: opened.setdefault("url", url))
    apps.open_url("example.com")
    assert opened["url"] == "https://example.com"


def test_web_search_builds_query_url(monkeypatch):
    opened = {}
    monkeypatch.setattr(apps.webbrowser, "open", lambda url: opened.setdefault("url", url))
    apps.web_search("hello world")
    assert "q=hello+world" in opened["url"]


def test_close_app_reports_failure_cleanly(monkeypatch):
    class FakeProc:
        returncode = 1
    monkeypatch.setattr(apps.subprocess, "run", lambda *a, **k: FakeProc())
    msg = apps.close_app("nothere")
    assert "could not close" in msg.lower()
