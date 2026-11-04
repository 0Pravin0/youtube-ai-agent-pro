"""macOS readings (parsed from real tool output formats) and the ECO/BALANCED/HIGH QUALITY governor."""
from __future__ import annotations

from app import governor, macos

BATT_DISCHARGING = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=4653155)\t24%; discharging; 1:05 remaining present: true\n"
BATT_AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=4653155)\t100%; charged; 0:00 remaining present: true\n"
BATT_DESKTOP = "Now drawing from 'AC Power'\n"
THERM_OK = ("Note: No thermal warning level has been recorded\nNote: No performance warning level has been recorded\n"
            "Note: No CPU power status has been recorded\n")
THERM_HOT = "2026-11-04 15:02:11 +0530 Thermal warning level set to 2.\n2026-11-04 15:02:11 +0530 Performance warning level set to 1.\n"
DISPLAYS = ('{"SPDisplaysDataType":[{"_name":"Apple M1","sppci_cores":"8","sppci_model":"Apple M1",'
            '"spdisplays_mtlgpufamilysupport":"spdisplays_metal3","sppci_vendor":"sppci_vendor_Apple"}]}')


def test_parsers():
    assert macos.parse_battery(BATT_DISCHARGING) == {"on_ac": False, "percent": 24, "charging": False, "has_battery": True}
    assert macos.parse_battery(BATT_AC)["charging"] and macos.parse_battery(BATT_AC)["on_ac"]
    assert macos.parse_battery(BATT_DESKTOP) == {"on_ac": True, "percent": None, "charging": False, "has_battery": False}
    assert macos.parse_thermal(THERM_OK) == {"thermal_level": 0, "performance_level": 0, "cpu_speed_limit": 100}
    assert macos.parse_thermal(THERM_HOT)["thermal_level"] == 2
    assert macos.parse_pressure("4\n", "System-wide memory free percentage: 9%") == {"pressure": "critical", "free_percent": 9}
    d = macos.parse_displays(DISPLAYS)
    assert d["gpu"] == "Apple M1" and d["gpu_cores"] == "8" and d["metal"] == "Metal 3"


def _reading(batt=BATT_AC, therm=THERM_OK, level="1"):
    return {"battery": macos.parse_battery(batt), "thermal": macos.parse_thermal(therm),
            "memory": macos.parse_pressure(level, "free percentage: 50%")}


def test_governor_policies():
    assert governor.evaluate("BALANCED", _reading()) is None
    assert "Battery at 24%" in governor.evaluate("BALANCED", _reading(batt=BATT_DISCHARGING))
    assert governor.evaluate("HIGH_QUALITY", _reading(batt=BATT_DISCHARGING)) is None  # 24% >= 15%
    assert "power adapter" in governor.evaluate("ECO", _reading(batt=BATT_DISCHARGING.replace("24%", "90%")))
    assert "hot" in governor.evaluate("BALANCED", _reading(therm=THERM_HOT))
    assert "critical" in governor.evaluate("HIGH_QUALITY", _reading(level="4"))
    assert governor.evaluate("BALANCED", _reading(level="2")) is None and governor.evaluate("ECO", _reading(level="2"))
    assert governor.max_jobs("BALANCED", 8) == 1 and governor.max_jobs("BALANCED", 16) == 2 and governor.max_jobs("ECO", 16) == 1
    assert governor.render_preset("ECO") == "veryfast"


def test_governor_holds_and_releases_runner(monkeypatch):
    from app.worker.runner import Runner
    r = Runner(workers=1)
    g = governor.Governor()
    monkeypatch.setattr(macos, "live", lambda: _reading(batt=BATT_DISCHARGING))
    g.tick(r, None, keep_awake=True)
    assert "resources" in r.holds and "Battery" in g.reason
    monkeypatch.setattr(macos, "live", lambda: _reading())
    g.tick(r, None, keep_awake=True)
    assert "resources" not in r.holds and g.reason is None
    r.pool.shutdown(wait=False)


def test_mac_profiles():
    from app import hardware
    assert hardware.mac_profile(8) == "LOW" and hardware.mac_profile(16) == "MEDIUM" and hardware.mac_profile(32) == "HIGH"
