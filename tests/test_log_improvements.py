"""Log / warning improvements (docs/log-improvement-spec.md R1–R4), run entirely on the simulators.

Run: uv run --with pytest pytest tests
"""
import json
import logging
import time

import pytest

import scripted_sim  # noqa: F401  (sets sys.path for app/ and sensors/)
from scripted_sim import ScriptedPsu, ScriptedRangefinder

import sim_devices
import web_monitor as wm
from rangefinder.constants import rc_text
from rangefinder.core import LKIFError
from truplasma_package import controller as ctl

LABELS = {"label_metal": "Al", "label_electrolyte": "test", "label_specimen_shape": "square",
          "label_specimen_length": "10", "label_specimen_width": "10", "label_specimen_thickness": "1"}


def wait_for(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append((record.levelname, getattr(record, "src", wm.SRC_SYSTEM), record.getMessage(),
                          record.exc_info is not None))

    def find(self, text, src=None):
        return [r for r in self.rows if text in r[2] and (src is None or r[1] == src)]


@pytest.fixture
def records():
    h = Records()
    wm.log.addHandler(h)
    wm.log.setLevel(logging.INFO)
    yield h
    wm.log.removeHandler(h)


@pytest.fixture
def svc(tmp_path, monkeypatch, records):
    """A fresh MonitorService with every device simulated (no DLL / COM port is ever opened)."""
    monkeypatch.setattr(wm, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(wm, "open_serial", lambda *a, **k: pytest.fail("real serial port opened"))
    monkeypatch.setattr(wm, "LKIF2Device", lambda *a, **k: pytest.fail("real LKIF2 DLL opened"))
    monkeypatch.setattr(sim_devices, "SimLKIF2Device", ScriptedRangefinder)
    monkeypatch.setattr(wm, "SimTruPlasma", ScriptedPsu)
    monkeypatch.setattr(ScriptedRangefinder, "script", [(1e9, "VALID")])
    monkeypatch.setattr(ScriptedRangefinder, "instances", [])
    s = wm.MonitorService()
    s.simulated = set(sim_devices.SIM_KEYS)
    s.psu_ports = []

    def open_port():  # 和 _psu_open_port 模擬分支相同，只是留下參照讓測試能改電源訊息
        port = ScriptedPsu(seed=1)
        s.psu_ports.append(port)
        return port
    s.psu.port_factory = open_port
    yield s
    if s.phase == "running":
        s.request_stop("manual")
        wait_for(lambda: s.phase == "stopped", 20)
    s.psu.disconnect()


def start(s):
    body, code = s.start(dict(wm.DEFAULT_PARAMS, **LABELS), True)
    assert code == 200, body
    return s.recorder


def stop(s):
    assert s.request_stop("manual")
    assert wait_for(lambda: s.phase == "stopped", 30)


def rangefinder(s):
    """Wait until the rangefinder worker is reading VALID values, return its ScriptedRangefinder."""
    assert wait_for(lambda: s.sensors["distance"]["reason"] == wm.R_OK)
    return ScriptedRangefinder.instances[-1]


def exp_json(rec):
    with open(rec.path(f"experiment_{rec.experiment_id}.json"), encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# R1 保留儀器原始資訊
# ---------------------------------------------------------------------------
def test_rc_text_never_none():
    assert rc_text(0x2000) == "Open Device Failed"
    assert rc_text(0x2003) == "未知碼 0x2003"
    e = LKIFError("Read failed", 0x2008)
    assert e.rc == 0x2008 and "RC=0x2008" in str(e) and "None" not in str(e) and "未知碼 0x2008" in str(e)


def test_alarm_message_keeps_raw_next_to_translation():
    msg = ctl.alarm_message({"code": 61657, "text": "溫度警告", "raw": "Mod=2:Overtemp warn,4512", "level": "warning"})
    assert "61657" in msg and "溫度警告" in msg and "Mod=2:Overtemp warn,4512" in msg
    msg = ctl.alarm_message({"code": 61999, "text": "Mod=0: Foo", "raw": "Mod=0: Foo", "level": "alarm"})
    assert "61999" in msg and "Mod=0: Foo" in msg


def test_controller_reports_raw_alarm_text():
    sim = ScriptedPsu(seed=1)
    events, alarms = [], []
    c = ctl.TruPlasmaController(lambda: sim, cycle_s=0.1, on_event=lambda lv, m: events.append((lv, m)),
                                on_alarm=alarms.append)
    c.connect()
    try:
        assert wait_for(lambda: c.phase == ctl.IDLE)
        sim.set_message(61657, "Mod=2:Overtemp warn,4512")
        assert wait_for(lambda: alarms)
        assert alarms[0]["code"] == 61657 and alarms[0]["raw"] == "Mod=2:Overtemp warn,4512"
        assert any("Mod=2:Overtemp warn,4512" in m for _, m in events)
    finally:
        c.disconnect()


def test_rangefinder_rc_codes_in_json_and_log(svc, records):
    rec = start(svc)
    rangefinder(svc).set_script([(0.3, 0x2003), (0.3, 0x2008), (1e9, "VALID")])
    assert wait_for(lambda: any(c["code"] == "RC=0x2008" for c in rec.info.get("device_codes", [])))
    stop(svc)
    codes = {c["code"]: c for c in exp_json(rec)["device_codes"] if c["source"] == "距離"}
    assert codes["RC=0x2003"]["value"] == 0x2003 and codes["RC=0x2003"]["text"] == "未知碼 0x2003"
    assert codes["RC=0x2008"]["count"] >= 1
    assert not [r for r in records.rows if "(None)" in r[2]]
    assert records.find("RC=0x2003")


def test_psu_raw_message_in_json(svc):
    svc.psu_connect("SIM")
    rec = start(svc)
    svc.psu_ports[-1].set_message(61657, "Mod=2:Overtemp warn,4512")
    assert wait_for(lambda: any(c["source"] == wm.PSU_NAME for c in rec.info.get("device_codes", [])))
    stop(svc)
    entry = next(c for c in exp_json(rec)["device_codes"] if c["source"] == wm.PSU_NAME)
    assert entry["value"] == 61657 and "Mod=2:Overtemp warn,4512" in entry["raw"]

