"""正式實驗流程（docs/plans/experiment-ux-flow.md 已決定 2026-10-31／已釐清 2026-11-02）的核心版，全部跑在模擬器上。

Run: uv run --with pytest pytest tests
"""
import csv
import json
import time

import pytest

import scripted_sim  # noqa: F401  (sets sys.path for app/ and sensors/)
from scripted_sim import ScriptedPsu, ScriptedRangefinder

import sim_devices
import web_monitor as wm
from truplasma_package import controller as ctl

LABELS = {"label_metal": "Al", "label_electrolyte": "test", "label_specimen_shape": "square",
          "label_specimen_length": "10", "label_specimen_width": "10", "label_specimen_thickness": "1"}
RECIPE = {"mode": "bipulse", "freq_hz": 1000, "pos_pct": 45, "neg_pct": 45, "u": 100, "i": 2, "p": 1}


def wait_for(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def body(exp_time="1.5", **kw):
    return dict(wm.DEFAULT_PARAMS, **LABELS, recipe=dict(RECIPE), exp_time=exp_time, psu_port="SIM", **kw)


def exp_json(rec):
    with open(rec.path(f"experiment_{rec.experiment_id}.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """全部裝置模擬的 MonitorService；基線與試收縮短（預設值另有測試），主繼電器穩定時間縮短。"""
    monkeypatch.setattr(wm, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(wm, "open_serial", lambda *a, **k: pytest.fail("real serial port opened"))
    monkeypatch.setattr(wm, "LKIF2Device", lambda *a, **k: pytest.fail("real LKIF2 DLL opened"))
    monkeypatch.setattr(sim_devices, "SimLKIF2Device", ScriptedRangefinder)
    monkeypatch.setattr(ScriptedRangefinder, "script", [(1e9, "VALID")])
    monkeypatch.setattr(ScriptedRangefinder, "instances", [])
    monkeypatch.setattr(ctl, "RELAYS_SETTLE_S", 0.3)
    s = wm.MonitorService()
    s.simulated = set(sim_devices.SIM_KEYS)
    s.exp_baseline_s = 0.5
    s.sensor_check_s = 0.5
    s.psu_ports = []

    def open_port():
        port = ScriptedPsu(seed=1)
        s.psu_ports.append(port)
        return port
    s.psu.port_factory = open_port
    monkeypatch.setattr(wm, "service", s)  # Flask 路由用的全域 service
    yield s
    if s.phase == "running":
        s.psu.emergency_stop()
        s.request_stop("manual")
        wait_for(lambda: s.phase == "stopped", 20)
    s.psu.disconnect()


@pytest.fixture
def client(svc):
    return wm.app.test_client()


def check_ok(s, **kw):
    res, code = s.exp_check(body(**kw))
    assert code == 200, res
    return res


def start_exp(s, exp_time="1.5", **kw):
    check_ok(s, exp_time=exp_time)
    res, code = s.exp_start(body(exp_time=exp_time, confirm_output=True, **kw))
    assert code == 200, res
    return s.recorder


def wait_stage(s, stage, timeout=15):
    assert wait_for(lambda: s.exp and s.exp["stage"] == stage, timeout), s.exp and s.exp["stage"]


def wait_stopped(s, timeout=30):
    assert wait_for(lambda: s.phase == "stopped", timeout), s.phase


# ---------------------------------------------------------------------------
def test_defaults_are_5s_baseline_and_3s_check():
    assert wm.EXP_BASELINE_S == 5.0 and wm.SENSOR_CHECK_S == 3.0
    s = wm.MonitorService()
    assert s.exp_baseline_s == 5.0 and s.sensor_check_s == 3.0


def test_parse_exp_time():
    assert wm.parse_exp_time("10:00") == 600
    assert wm.parse_exp_time("1:30") == 90
    assert wm.parse_exp_time("45") == 45
    for bad in ("", "abc", "1:75", "0", "-3", "1:-5"):
        with pytest.raises(ValueError):
            wm.parse_exp_time(bad)


def test_labels_and_time_required(svc):
    res, code = svc.exp_check(dict(body(), label_metal=""))
    assert code == 400 and any("金屬種類" in e for e in res["errors"])
    res, code = svc.exp_check(body(exp_time=""))
    assert code == 400 and any("實驗時間" in e for e in res["errors"])
    assert not svc.psu.connected  # 驗證沒過，不碰電源


def test_check_all_ok_writes_and_reads_back_recipe(svc):
    t0 = time.monotonic()
    res = check_ok(svc)
    assert time.monotonic() - t0 >= svc.sensor_check_s
    assert res["psu_ok"] and res["can_start"] and res["missing"] == []
    assert all(v["ok"] and v["valid"] > 0 for v in res["sensors"].values()), res["sensors"]
    rb = res["readback"]
    assert rb["channels"]["33"] == 32 and rb["channels"]["51131"] == 50
    assert rb["setpoints"] == {"u": 100.0, "i": 2.0, "p": 1.0}
    assert svc.phase == "idle" and svc.psu.phase == "idle"


# 電源不正常不能開始 --------------------------------------------------------
def test_psu_unreachable_cannot_start(svc, tmp_path):
    def fail():
        raise OSError("could not open port SIM: 拒絕存取")
    svc.psu.port_factory = fail
    res = check_ok(svc)
    assert not res["psu_ok"] and not res["can_start"]
    assert any("電源連線失敗" in e for e in res["psu_errors"])
    res, code = svc.exp_start(body(confirm_output=True, confirm_missing=True))
    assert code == 400 and svc.recorder is None and svc.phase == "idle"
    assert not list(tmp_path.glob("EXP_*"))  # 沒有開始錄製


def test_psu_interlock_cannot_start(svc, tmp_path):
    svc.psu_connect("SIM")
    svc.psu_ports[-1].interlock = True
    assert wait_for(lambda: svc.psu.snapshot()["status"]["interlock"])
    res = check_ok(svc)
    assert not res["can_start"] and any("Interlock" in e for e in res["psu_errors"])
    res, code = svc.exp_start(body(confirm_output=True, confirm_missing=True))
    assert code == 400 and any("Interlock" in e for e in res["errors"])
    assert svc.recorder is None and not list(tmp_path.glob("EXP_*"))


def test_start_requires_check_and_confirm(svc):
    res, code = svc.exp_start(body(confirm_output=True))
    assert code == 400 and "檢驗Sensor與電供" in res["errors"][0]
    check_ok(svc)
    res, code = svc.exp_start(body())
    assert code == 400 and "confirm_output" in res["errors"][0]


# sensor 異常可強制開始，manifest 標記缺少 ------------------------------------
def test_sensor_fault_warns_and_force_start_marks_missing(svc, monkeypatch):
    class BrokenSpec:
        def __init__(self, *a, **k):
            raise RuntimeError("沒有找到光譜儀!（測試）")
    monkeypatch.setattr(sim_devices, "SimSpectrometer", BrokenSpec)
    res = check_ok(svc, exp_time="0.5")  # 開始時的參數必須和檢驗時相同
    assert res["psu_ok"] and res["can_start"]
    assert [m["key"] for m in res["missing"]] == ["spectrometer"]
    assert res["missing"][0]["reason"] != wm.R_OK and res["missing"][0]["detail"]
    res, code = svc.exp_start(body(exp_time="0.5", confirm_output=True))
    assert code == 409 and res["needs_confirm"] and res["missing"][0]["key"] == "spectrometer"
    assert svc.recorder is None
    res, code = svc.exp_start(body(exp_time="0.5", confirm_output=True, confirm_missing=True))
    assert code == 200, res
    rec = svc.recorder
    wait_stopped(svc)
    doc = exp_json(rec)
    assert doc["mode"] == "experiment"
    assert [m["key"] for m in doc["missing_sensors"]] == ["spectrometer"]
    assert doc["result"]["code"] == "completed"
    assert any("缺少感測器：光譜儀" in n["message"] for n in svc.notices)  # 結束摘要


# 開始順序：錄製比輸出 ON 早 ≥5 秒（預設基線） ---------------------------------
def test_recording_starts_at_least_5s_before_output_on(svc):
    svc.exp_baseline_s = wm.EXP_BASELINE_S
    rec = start_exp(svc, exp_time="1")
    assert svc.phase == "running" and svc.psu.phase == "idle"  # 開始錄製時電源仍關閉
    time.sleep(3)
    assert svc.exp["stage"] == "baseline" and svc.psu.phase == "idle"
    wait_stopped(svc, 40)
    doc = exp_json(rec)
    assert doc["output"]["record_to_on_s"] >= 5.0, doc["output"]
    on_unix = svc.exp["output_on_at"]
    assert on_unix - doc["start_time_unix"] >= 5.0
    with open(rec.path(f"temperature_{rec.experiment_id}.csv"), encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert any(float(r["Elapsed(s)"]) < 4.5 and r["Status"] == "ok" for r in rows)  # 通電前已有感測器資料
    with open(rec.path(f"truplasma_{rec.experiment_id}.csv"), encoding="utf-8-sig") as f:
        psu_rows = list(csv.DictReader(f))
    first_on = next(float(r["Elapsed(s)"]) for r in psu_rows if r["PowerOn"] == "1")
    assert first_on >= 5.0
    assert doc["power_supply_settings"]["readback_before_start"]["setpoints"] == {"u": 100.0, "i": 2.0, "p": 1.0}
    assert doc["experiment_time_s"] == 1.0 and doc["baseline_s"] == 5.0


# 定時器到：自動關輸出並立刻停止錄製 ----------------------------------------
def test_timer_turns_output_off_then_stops_recording(svc):
    rec = start_exp(svc, exp_time="1.5")
    wait_stage(svc, "on")
    assert svc.psu.phase == "on"
    wait_stopped(svc)
    assert svc.psu.phase == "idle"
    st = svc.psu.snapshot()["status"]
    assert not st["power_on"] and not st["relays_on"]
    doc = exp_json(rec)
    assert doc["result"]["code"] == "completed" and doc["stop_reason"] == "計時結束" and doc["status"] == "completed"
    assert doc["psu_logging"] == "controlled"
    assert 1.5 <= doc["output"]["on_s"] < 3.5, doc["output"]
    off_unix = svc.exp["output_off_at"]
    assert 0 <= doc["end_time_unix"] - off_unix < 2.0  # 不多錄
    assert not svc.exp_active()


# 緊急停止後錄製持續 --------------------------------------------------------
def test_estop_turns_output_off_and_recording_continues(svc, client):
    rec = start_exp(svc, exp_time="60")
    wait_stage(svc, "on")
    r = client.post("/api/exp/estop")
    assert r.status_code == 200
    assert wait_for(lambda: svc.psu.phase == "idle", 5)
    time.sleep(1.5)
    assert svc.phase == "running" and svc.exp["stage"] == "estop"  # 感測器繼續錄
    assert svc.exp["result"] == "aborted_user"
    n_rows = rec.files["temperature"]["rows"]
    time.sleep(1.2)
    assert rec.files["temperature"]["rows"] > n_rows
    r = client.post("/api/stop")  # 使用者按「停止錄製」
    assert r.status_code == 202
    wait_stopped(svc)
    doc = exp_json(rec)
    assert doc["result"]["code"] == "aborted_user" and doc["result"]["detail"] == "緊急停止"
    assert doc["output"]["off_time"] is not None


# 實驗中改 U/I/P 被拒絕 -----------------------------------------------------
def test_setpoint_changes_rejected_during_experiment(svc, client):
    start_exp(svc, exp_time="60")
    wait_stage(svc, "on")
    for path, payload in (("/api/psu/setpoints", {"u": 500, "i": 5, "p": 2}),
                          ("/api/psu/recipe", dict(RECIPE, u=500)),
                          ("/api/psu/param", {"channel": 51131, "value": 30}),
                          ("/api/psu/output", {"on": True}),
                          ("/api/psu/disconnect", {})):
        r = client.post(path, json=payload)
        assert r.status_code == 400, path
        assert r.get_json()["errors"] == [wm.EXP_PSU_LOCKED], path
    assert svc.psu.snapshot()["setpoints"] == {"u": 100.0, "i": 2.0, "p": 1.0}
    assert svc.psu.phase == "on"
    r = client.post("/api/exp/end")
    assert r.status_code == 202
    wait_stopped(svc)
    assert svc.exp["result"] == "aborted_user"
    # 實驗結束後解除鎖定
    r = client.post("/api/psu/setpoints", json={"u": 50, "i": 1, "p": 1})
    assert r.status_code == 200


# 警報後無續跑 --------------------------------------------------------------
def test_psu_alarm_aborts_without_resume(svc, client):
    rec = start_exp(svc, exp_time="60")
    wait_stage(svc, "on")
    svc.psu_ports[-1].set_message(61625, "Arc limit exceeded", "alarm")
    wait_stage(svc, "psu_fault")
    assert wait_for(lambda: svc.psu.phase == "idle", 5)
    assert svc.phase == "running"  # 錄製繼續
    assert svc.exp["result"] == "aborted_psu_alarm"
    svc.psu_ports[-1].set_message(None, "")  # 警報清掉也不能續跑
    for path, payload in (("/api/psu/reset-alarms", {}), ("/api/psu/output", {"on": True})):
        r = client.post(path, json=payload)
        assert r.status_code == 400 and r.get_json()["errors"] == [wm.EXP_PSU_LOCKED], path
    r = client.post("/api/exp/start", json=body(confirm_output=True, confirm_missing=True))
    assert r.status_code == 409
    time.sleep(1)
    assert svc.psu.phase == "idle" and svc.exp["stage"] == "psu_fault"
    r = client.post("/api/exp/end")  # 「結束這場」
    assert r.status_code == 202
    wait_stopped(svc)
    doc = exp_json(rec)
    assert doc["result"]["code"] == "aborted_psu_alarm" and "61625" in doc["result"]["detail"]


# Sensor 測試模式：不呼叫任何電源命令，manifest 標 sensor_test -----------------
def test_sensor_test_mode_never_touches_psu(svc, client, tmp_path):
    calls = []
    names = ("connect", "disconnect", "power_on", "power_off", "emergency_stop", "set_setpoints",
             "write_param", "read_params", "reset_alarms", "reset_counters", "_submit")
    for name in names:
        setattr(svc.psu, name, lambda *a, _n=name, **k: calls.append(_n))
    svc.psu.port_factory = lambda: calls.append("port_factory")
    raw = dict(wm.DEFAULT_PARAMS, confirm=True)  # 標籤可不填
    r = client.post("/api/start", json=raw)
    assert r.status_code == 200, r.get_json()
    rec = svc.recorder
    assert svc.snapshot()["mode"] == "sensor_test"
    time.sleep(1.5)
    r = client.post("/api/stop")
    assert r.status_code == 202
    wait_stopped(svc)
    assert calls == []
    doc = exp_json(rec)
    assert doc["mode"] == "sensor_test" and doc["experiment_labels"] is None
    assert doc["devices"]["power_supply"] is None and "result" not in doc and doc["psu_logging"] == "none"
    assert svc.exp is None
    for name in names:
        delattr(svc.psu, name)


# 檢驗時自動重設鎖存警報（白名單：61613 與 Test version 家族；其他照擋） ---------------------


def _latch(svc, code):
    """連線後讓模擬電源鎖存警報 code，等網頁端讀到代碼。"""
    svc.psu_connect("SIM")
    svc.psu_ports[-1]._trip(code)
    assert wait_for(lambda: (svc.psu.snapshot()["alarm"] or {}).get("code") == code)


def _count_resets(svc, monkeypatch):
    sent = []
    real = svc.psu.reset_alarms
    monkeypatch.setattr(svc.psu, "reset_alarms", lambda: (sent.append(1), real())[1])
    return sent


@pytest.mark.parametrize("code", [61613, 61714])
def test_check_auto_resets_whitelisted_alarm(svc, monkeypatch, code):
    _latch(svc, code)
    sent = _count_resets(svc, monkeypatch)
    res = check_ok(svc)
    assert sent == [1] and res["psu_ok"], res["psu_errors"]
    assert not svc.psu.snapshot()["status"]["alarm_active"]


def test_check_does_not_reset_other_alarm(svc, monkeypatch):
    _latch(svc, 61624)  # 短路：可能是真問題，不自動重設
    sent = _count_resets(svc, monkeypatch)
    res = check_ok(svc)
    assert sent == [] and not res["psu_ok"]
    assert any("61624" in e for e in res["psu_errors"]) and not any("自動重設" in e for e in res["psu_errors"])


def test_check_blocks_with_note_when_reset_does_not_clear(svc, monkeypatch):
    monkeypatch.setattr(wm, "AUTO_RESET_WAIT_S", 1.0)
    svc.psu_connect("SIM")
    svc.psu_ports[-1].set_message(61613, "ActSourceFail", "alarm")  # 重設清不掉
    assert wait_for(lambda: (svc.psu.snapshot()["alarm"] or {}).get("code") == 61613)
    sent = _count_resets(svc, monkeypatch)
    res = check_ok(svc)
    assert sent == [1] and not res["psu_ok"]
    assert any("已自動重設一次仍未清除" in e for e in res["psu_errors"])
