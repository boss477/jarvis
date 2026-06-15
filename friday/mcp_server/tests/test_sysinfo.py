from types import SimpleNamespace

from friday.mcp_server import sysinfo


def _patch(monkeypatch, *, battery, plugged, cpu, mem_pct, used, total):
    batt = None if battery is None else SimpleNamespace(percent=battery, power_plugged=plugged)
    monkeypatch.setattr(sysinfo.psutil, "sensors_battery", lambda: batt)
    monkeypatch.setattr(sysinfo.psutil, "cpu_percent", lambda interval=None: cpu)
    monkeypatch.setattr(
        sysinfo.psutil, "virtual_memory",
        lambda: SimpleNamespace(percent=mem_pct, used=used, total=total),
    )


def test_battery_on_charge(monkeypatch):
    _patch(monkeypatch, battery=85, plugged=True, cpu=12.0, mem_pct=40.0,
           used=6.4e9, total=16e9)
    msg = sysinfo.system_info()
    assert "Battery 85% (charging)" in msg
    assert "CPU 12%" in msg
    assert "RAM 40%" in msg


def test_battery_discharging(monkeypatch):
    _patch(monkeypatch, battery=30, plugged=False, cpu=5.0, mem_pct=50.0,
           used=8e9, total=16e9)
    assert "30% (on battery)" in sysinfo.system_info()


def test_no_battery(monkeypatch):
    _patch(monkeypatch, battery=None, plugged=False, cpu=5.0, mem_pct=50.0,
           used=8e9, total=16e9)
    assert "No battery" in sysinfo.system_info()
