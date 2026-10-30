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



# ---------------------------------------------------------------------------
# R2 暫時失誤與真故障分開
# ---------------------------------------------------------------------------
def test_tracker_needs_count_and_duration():
    tr = wm.FailureTracker()
    t = 1000.0
    for i in range(30):  # 30 次但只有 2.9 秒：不升級
        assert tr.fail(t + i * 0.1, wm.R_WIRING, "ALARM", False, "x") is None
    assert not tr.escalated
    assert tr.fail(t + 5.0, wm.R_WIRING, "ALARM", False, "x") == "escalate"
    tr2 = wm.FailureTracker()
    for i in range(9):  # 9 次、跨 8 秒：次數不夠也不升級
        assert tr2.fail(t + i, wm.R_NO_DATA, "no_data", False, "x") is None
    assert tr2.fail(t + 9, wm.R_NO_DATA, "no_data", False, "x") == "escalate"
    tr3 = wm.FailureTracker()
    assert tr3.fail(t, wm.R_NO_DATA, "全為 0", False, "x", confirmed=True) == "escalate"


def test_tracker_transient_rate_limit():
    tr = wm.FailureTracker()
    logged = []
    for i in range(12):
        tr.fail(100.0 + i, wm.R_ERROR, "RC=0x2003", True, "x")
        kind, ep = tr.ok(100.2 + i)
        assert kind == "transient"
        logged.append(tr.transient_logged(100.2 + i, ep)[0])
    assert logged.count(True) == wm.TRANSIENT_LOG_MAX
    assert tr.flush_suppressed()[0] == 12 - wm.TRANSIENT_LOG_MAX
    assert tr.transients == 12 and tr.transient_codes["RC=0x2003"] == 12


def _sensor_rows(records, src, levels=("WARNING", "ERROR")):
    return [r for r in records.rows if r[1] == src and r[0] in levels]


def test_transient_rc_blip_is_one_info_line(svc, records):
    rec = start(svc)
    dev = rangefinder(svc)
    n_notices = len(svc.notices)
    dev.set_script([(0.25, 0x2003), (0.25, 0x2008), (1e9, "VALID")])
    assert wait_for(lambda: records.find("短暫讀取失敗", "距離"))
    time.sleep(0.5)
    lines = records.find("短暫讀取失敗", "距離")
    assert len(lines) == 1 and lines[0][0] == "INFO" and not lines[0][3]
    assert "RC=0x2003→RC=0x2008" in lines[0][2]
    assert not _sensor_rows(records, "距離")
    assert len(svc.notices) == n_notices
    stop(svc)
    summary = records.find("短暫失誤彙總", "距離")
    assert summary and "RC=0x2003" in summary[0][2] and "最長持續" in summary[0][2]
    fs = exp_json(rec)["failure_summary"]["距離"]["transient"]
    assert fs["count"] == 1 and fs["codes"]["RC=0x2008"] >= 1


def test_persistent_alarm_escalates_once_then_recovers(svc, records):
    rec = start(svc)
    dev = rangefinder(svc)
    dev.set_script([(6.5, "ALARM"), (1e9, "VALID")])
    assert wait_for(lambda: records.find("恢復正常", "距離"), 15)
    esc = records.find("持續失敗", "距離")
    assert len(esc) == 1 and esc[0][0] == "WARNING"
    notices = [n for n in svc.notices if n["title"].startswith("距離")]
    assert [n["level"] for n in notices] == ["warning", "info"]
    stop(svc)
    kinds = [e["kind"] for e in exp_json(rec)["events"] if e["source"] == "距離"]
    assert kinds == ["escalated", "recovered"]


def test_failing_at_stop_is_reported(svc, records):
    rec = start(svc)
    rangefinder(svc).set_script([(1e9, 0x2008)])
    assert wait_for(lambda: records.find("持續失敗", "距離"), 15)
    esc = records.find("持續失敗", "距離")[0]
    assert esc[0] == "ERROR" and esc[3]  # 通訊層錯誤記 ERROR，traceback 只在升級時附一次
    assert sum(r[3] for r in records.rows if r[1] == "距離") == 1
    stop(svc)
    still = records.find("停止時仍在失敗", "距離")
    assert still and still[0][0] == "WARNING" and "RC=0x2008" in still[0][2]
    assert exp_json(rec)["failure_summary"]["距離"]["failing_at_stop"]["codes"]["RC=0x2008"] > 10


# ---------------------------------------------------------------------------
# R3 去重與等級一致
# ---------------------------------------------------------------------------
TEST_VERSION_TEXT = "Mod=0: Test version. Internal use only."


@pytest.fixture
def psu_rig(records):
    sim = ScriptedPsu(seed=1)
    events = []
    c = ctl.TruPlasmaController(lambda: sim, cycle_s=0.1, on_event=lambda lv, m: events.append((lv, m)))
    c.connect()
    assert wait_for(lambda: c.phase == ctl.IDLE)
    yield c, sim, events
    c.disconnect()


def test_test_version_family_is_quiet_and_logged_once(psu_rig, records):
    c, sim, events = psu_rig
    sim.set_message(61714, TEST_VERSION_TEXT, "alarm")
    assert wait_for(lambda: c.alarm and c.alarm["code"] == 61714)
    sim.set_message(61711, TEST_VERSION_TEXT, "warning")
    assert wait_for(lambda: c.alarm and c.alarm["code"] == 61711)
    sim.set_message(61714, TEST_VERSION_TEXT, "alarm")
    time.sleep(0.5)
    tv = [e for e in events if "Test version" in e[1]]
    assert [lv for lv, _ in tv] == ["quiet", "quiet"]  # 61714、61711 各一次，61714 再出現不重寫
    assert all("61704／61711／61714" in m and TEST_VERSION_TEXT in m for _, m in tv)
    assert "Test ver.Internal use only" in tv[0][1]  # 手冊原文
    assert not [m for _, m in tv if "故障" in m or "測試版" in m]
    assert c.alarm["level"] == "info" and c.alarm["category"] == "test_version"
    assert all(r[0] == "INFO" for r in records.find("Test version"))


def test_real_warning_not_relogged_when_alternating_with_test_version(psu_rig):
    c, sim, events = psu_rig
    for code, text in ((61657, "Mod=2:Overtemp warn,4512"), (61711, TEST_VERSION_TEXT),
                       (61657, "Mod=2:Overtemp warn,4512")):
        sim.set_message(code, text)
        assert wait_for(lambda: c.alarm and c.alarm["code"] == code)
    time.sleep(0.3)
    assert len([e for e in events if "61657" in e[1]]) == 1


def test_psu_event_lines_have_same_level(svc, records):
    svc.psu_connect("SIM")
    svc.psu_ports[-1].set_message(61657, "Mod=2:Overtemp warn,4512")
    assert wait_for(lambda: len(records.find("61657")) >= 2)
    svc.psu_ports[-1].set_message(61714, TEST_VERSION_TEXT, "alarm")
    assert wait_for(lambda: len(records.find("61714")) >= 2)
    for code in ("61657", "61714"):
        rows = records.find(code)
        assert {r[1] for r in rows} == {wm.SRC_SYSTEM, wm.PSU_NAME}, rows
        assert len({r[0] for r in rows}) == 1, rows
    assert records.find("61657")[0][0] == "WARNING" and records.find("61714")[0][0] == "INFO"
    assert not [n for n in svc.notices if "61714" in n["message"]]
    assert [n for n in svc.notices if "61657" in n["message"]]


class _NoRangefinder:
    def __init__(self, *a, **k):
        pass

    def open(self):
        raise LKIFError("OpenDevice 失敗", 0x2000)


def test_preflight_logged_once_and_failure_in_one_line(svc, records, monkeypatch):
    monkeypatch.setattr(wm, "LKIF2Device", _NoRangefinder)
    svc.simulated = set(sim_devices.SIM_KEYS) - {"distance"}
    raw = dict(wm.DEFAULT_PARAMS, **LABELS)
    svc.preflight(raw)
    svc.preflight(raw)
    assert len(records.find("預檢結果")) == 1
    assert "通過，但停用 距離" in records.find("預檢結果")[0][2]
    rows = [r for r in records.rows if r[1] == "距離"]
    assert len(rows) == 1 and "RC=0x2000" in rows[0][2] and "停用" in rows[0][2]
    assert not records.find("預檢警告")


# ---------------------------------------------------------------------------
# R4 網頁文字
# ---------------------------------------------------------------------------
def test_alarm_text_names_head_and_never_says_disconnect(svc, records):
    start(svc)
    rangefinder(svc).set_script([(1e9, "ALARM")])
    assert wait_for(lambda: records.find("持續失敗", "距離"), 15)
    card = svc.sensors["distance"]
    assert card["detail"] == f"HEAD {wm.RANGEFINDER_HEAD} 收不到反射光（ALARM）：檢查距離、角度、表面反光，或換 HEAD"
    assert card["text"] == "沒有有效值" and card["level"] == "error"
    head, advice = wm.split_detail(card["detail"])
    assert head.endswith("（ALARM）") and "換 HEAD" in advice
    toast = [n for n in svc.notices if n["title"].startswith("距離")][-1]
    assert head in toast["message"] and "原因與建議見狀態卡" in toast["message"]
    said = [r[2] for r in records.rows if r[1] == "距離"] + [n["message"] for n in svc.notices]
    assert not [t for t in said if "斷線" in t or "USB" in t]


def test_usb_wording_only_for_communication_rc():
    code, detail, usb = wm.rangefinder_read_error(LKIFError("Read failed", 0x2001))
    assert usb and code == "RC=0x2001" and "USB 可能中斷" in detail and "RC=0x2001" in detail
    for rc in (0x2003, 0x2008, 0x1005):  # 未知碼：照實寫「意思待查」，不猜 USB
        code, detail, usb = wm.rangefinder_read_error(LKIFError("Read failed", rc))
        assert not usb and f"RC=0x{rc:04X}" in detail and "意思待查" in detail and "USB 可能" not in detail
        assert "不是 USB" not in detail and "可不處理" not in detail
    code, detail, usb = wm.rangefinder_read_error(ValueError("boom"))
    assert not usb and code == "ValueError" and "USB" not in detail
    for status, (_, text) in wm.RANGEFINDER_STATUS_DIAG.items():
        assert "斷線" not in text and "USB" not in text and f"（{status}" in text.replace("過遠，", "").replace("過近，", "")


def test_usb_disconnect_card_and_toast(svc, records):
    start(svc)
    rangefinder(svc).set_script([(1e9, 0x2001)])
    assert wait_for(lambda: records.find("持續失敗", "距離"), 15)
    card = svc.sensors["distance"]
    assert card["text"] == "通訊中斷" and "RC=0x2001" in card["detail"] and "USB 可能中斷" in card["detail"]
    toast = [n for n in svc.notices if n["title"].startswith("距離")][-1]
    assert toast["level"] == "error" and "RC=0x2001" in toast["message"]


def test_61657_wording():
    msg = ctl.alarm_message({"code": 61657, "text": "電源溫度警告", "raw": "Mod=2:Overtemp warn,4512",
                             "level": "warning"})
    assert "電源溫度警告（原文：Mod=2:Overtemp warn,4512）" in msg
    assert "過溫" not in msg and "過熱" not in msg
    from truplasma_package.protocol import ALARM_ADVICE
    assert "風扇" in ALARM_ADVICE[61657] and "過溫" not in ALARM_ADVICE[61657]
    from truplasma_package.protocol import ALARM_TEXT
    assert ALARM_TEXT[61657] == "電源溫度警告"


def test_card_detail_has_no_sensor_name_prefix():
    assert wm.MonitorService._disabled_detail("距離", "LK-G5000 未連線：檢查 USB 線") == "LK-G5000 未連線：檢查 USB 線"
    assert wm.split_detail("短現象：原因：再細分") == ("短現象", "原因：再細分")
    assert wm.split_detail("沒有冒號") == ("沒有冒號", "")


def test_psu_raw_change_and_clear_are_logged_quietly(psu_rig, records):
    c, sim, events = psu_rig
    sim.set_message(61657, "Mod=2:Overtemp warn,4512")
    assert wait_for(lambda: c.alarm and c.alarm["raw"].endswith("4512"))
    sim.set_message(61711, TEST_VERSION_TEXT)
    assert wait_for(lambda: c.alarm and c.alarm["code"] == 61711)
    sim.set_message(61657, "Mod=2:Overtemp warn,4530")
    assert wait_for(lambda: any("4530" in m for _, m in events))
    sim.set_message(61714, TEST_VERSION_TEXT.replace("Mod=0", "Mod=1"), "alarm")
    assert wait_for(lambda: any("Mod=1" in m for _, m in events))
    sim.set_message(0, "")
    assert wait_for(lambda: any("已消失" in m for _, m in events))
    assert [lv for lv, m in events if "61657" in m and "原文更新" not in m and "已消失" not in m] == ["warning"]
    assert all(lv == "quiet" for lv, m in events if "原文更新" in m or "已消失" in m or "Test version" in m)
    advice = [r for r in records.rows if r[1] == wm.SRC_SYSTEM and "61657" in r[2] and "風扇" in r[2]]
    assert advice, "advice belongs in the controller's log line"
    assert not [m for _, m in events if "風扇" in m], "notice text stays short"


def test_alarm_output_drop_has_alarm_level(psu_rig):
    c, sim, events = psu_rig
    c.set_setpoints(200, 2, 1)
    c.power_on()
    assert wait_for(lambda: c.phase == ctl.ON)
    sim.set_message(61624, "Mod=0: Short circuit", "alarm")
    assert wait_for(lambda: any("61624" in m for _, m in events))
    drop = [lv for lv, m in events if "輸出已關閉" in m]
    assert drop and drop[0] == "error"


def test_confirmed_failure_counts_from_its_real_start():
    tr = wm.FailureTracker()
    assert tr.fail(100.0, wm.R_NO_DATA, "全為 0", False, "x", confirmed=True, since=97.0) == "escalate"
    assert tr.ok(102.0)[1]["duration"] == 5.0
