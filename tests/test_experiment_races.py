"""正式實驗流程的競態與修正回歸測試（驗收報告 core-flow-verify.md 的重現測試移入，加上修正第 1 輪的各項）。

Run: uv run --with pytest pytest tests
"""
import json
import threading
import time

import pytest

import scripted_sim  # noqa: F401  (sets sys.path for app/ and sensors/)
from scripted_sim import ScriptedPsu, ScriptedRangefinder

import sim_devices
import web_monitor as wm
from truplasma_package import controller as ctl

LABELS = {"label_metal": "Al", "label_electrolyte": "test", "label_specimen_shape": "square",
          "label_specimen_length": "10", "label_specimen_width": "10", "label_specimen_thickness": "1"}
RECIPE = {"mode": "bipulse", "freq_hz": 1000, "duty_pct": 50, "u": 100, "i": 2, "p": 1}
OUTPUT_PHASES = ("relays", "powering", "on")


def wait_for(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def body(exp_time="30", **kw):
    return dict(wm.DEFAULT_PARAMS, **LABELS, recipe=dict(RECIPE), exp_time=exp_time, psu_port="SIM", **kw)


def exp_json(rec):
    with open(rec.path(f"experiment_{rec.experiment_id}.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(wm, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(wm, "open_serial", lambda *a, **k: pytest.fail("real serial"))
    monkeypatch.setattr(wm, "LKIF2Device", lambda *a, **k: pytest.fail("real LKIF2"))
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
    monkeypatch.setattr(wm, "service", s)
    yield s
    s.psu.emergency_stop()
    if s.phase == "running":
        s.request_stop("manual")
        wait_for(lambda: s.phase == "stopped", 20)
    s.psu.disconnect()


def start(s, **kw):
    res, code = s.exp_check(body(**kw))
    assert code == 200 and res["can_start"], res
    res, code = s.exp_start(body(confirm_output=True, **kw))
    assert code == 200, res


# ---------------------------------------------------------------------------
# 安全 1：緊急停止與開啟輸出的競態
# ---------------------------------------------------------------------------
def test_estop_between_check_and_power_on(svc, monkeypatch):
    """緊急停止落在基線結束的旗標檢查之後、psu.power_on() 之前（驗收重現測試）：輸出最後必須是關的。"""
    orig = svc._exp_update

    def hooked(exp, stage=None, message=None, **f):
        orig(exp, stage, message, **f)
        if stage == "starting":
            svc.exp_estop()
    monkeypatch.setattr(svc, "_exp_update", hooked)
    start(svc)
    time.sleep(3)
    snap = svc.psu.snapshot()
    assert snap["phase"] not in OUTPUT_PHASES, "OUTPUT ON AFTER ESTOP"
    assert not snap["status"]["power_on"] and not snap["status"]["relays_on"]
    assert svc.exp["stage"] == "estop" and svc.exp["result"] == "aborted_user"
    assert svc.phase == "running"  # 錄製繼續


def test_estop_right_after_power_on(svc, monkeypatch):
    """緊急停止緊接在 power_on() 之後到（controller 在 relays 階段）：輸出必須關閉、不能進入 on。"""
    orig = svc.psu.power_on

    def hooked():  # 緊急停止由另一個 thread（HTTP 請求）在 power_on 之後立刻送到
        orig()
        threading.Thread(target=svc.exp_estop).start()
    monkeypatch.setattr(svc.psu, "power_on", hooked)
    start(svc)
    assert wait_for(lambda: svc.exp["stage"] == "estop", 10)
    assert wait_for(lambda: svc.psu.phase == "idle", 8)
    time.sleep(2)
    snap = svc.psu.snapshot()
    assert snap["phase"] == "idle" and not snap["status"]["power_on"] and not snap["status"]["relays_on"]
    assert svc.exp["output_on_at"] is None  # 從沒進入輸出中


# ---------------------------------------------------------------------------
# 安全 2：exp_start 期間電源由實驗獨佔
# ---------------------------------------------------------------------------
def test_manual_output_on_during_exp_start_window(svc, monkeypatch):
    """讀回比對之後、開始錄製之前（驗收重現測試）：手動開輸出必須被拒絕。"""
    client = wm.app.test_client()
    orig_start = svc.start
    seen = {}

    def hooked_start(raw, confirmed, mode="sensor_test", extra_info=None):
        r = client.post("/api/psu/output", json={"on": True})
        seen["code"] = r.status_code
        seen["sp"] = client.post("/api/psu/setpoints", json={"u": 900, "i": 20, "p": 9}).status_code
        return orig_start(raw, confirmed, mode=mode, extra_info=extra_info)
    monkeypatch.setattr(svc, "start", hooked_start)
    start(svc)
    monkeypatch.setattr(svc, "start", orig_start)
    assert seen["code"] == 400, "manual output ON accepted while exp_start in progress"
    assert seen["sp"] == 400
    assert svc.psu.snapshot()["setpoints"] == {"u": 100.0, "i": 2.0, "p": 1.0}
    assert not wait_for(lambda: svc.psu.phase in OUTPUT_PHASES and svc.exp["stage"] == "baseline", 1)
    assert wait_for(lambda: svc.exp["stage"] == "on", 15)


def test_manual_changes_rejected_during_readback(svc, monkeypatch):
    """在 exp_start 做寫入讀回的當下（stage=preparing）就已經鎖住；失敗時釋放鎖定。"""
    client = wm.app.test_client()
    orig = svc._exp_apply_recipe
    seen = {}

    def hooked(recipe):
        seen["stage"] = svc.exp["stage"]
        seen["out"] = client.post("/api/psu/output", json={"on": True}).status_code
        seen["param"] = client.post("/api/psu/param", json={"channel": 51131, "value": 30}).status_code
        raise RuntimeError("模擬讀回不一致")
    res, code = svc.exp_check(body())
    assert code == 200
    monkeypatch.setattr(svc, "_exp_apply_recipe", hooked)
    res, code = svc.exp_start(body(confirm_output=True))
    assert code == 400 and "模擬讀回不一致" in res["errors"][0]
    assert seen == {"stage": "preparing", "out": 400, "param": 400}
    assert not svc.exp_active() and svc.recorder is None  # 失敗後釋放、沒有開始錄製
    assert client.post("/api/psu/setpoints", json={"u": 50, "i": 1, "p": 1}).status_code == 200


def test_manual_output_rejected_during_check(svc, monkeypatch):
    client = wm.app.test_client()
    orig = svc._sensor_trial
    seen = {}

    def hooked(raw):
        seen["out"] = client.post("/api/psu/output", json={"on": True}).status_code
        return orig(raw)
    monkeypatch.setattr(svc, "_sensor_trial", hooked)
    res, code = svc.exp_check(body())
    assert code == 200 and seen["out"] == 400 and svc.psu.phase == "idle"


# ---------------------------------------------------------------------------
# 實驗中電源異常（驗收重現測試）
# ---------------------------------------------------------------------------
def test_interlock_during_output(svc):
    start(svc)
    assert wait_for(lambda: svc.exp["stage"] == "on", 15)
    svc.psu_ports[-1].interlock = True
    assert wait_for(lambda: svc.exp["stage"] == "psu_fault", 10), svc.exp["stage"]
    assert wait_for(lambda: svc.psu.phase == "idle", 5)
    assert svc.exp["result"] == "aborted_interlock" and svc.phase == "running"


def test_comm_lost_during_output(svc):
    start(svc)
    assert wait_for(lambda: svc.exp["stage"] == "on", 15)
    port = svc.psu_ports[-1]
    port.read = lambda n: b""  # unit stops answering
    assert wait_for(lambda: svc.exp["stage"] == "psu_fault", 20), svc.exp["stage"]
    assert svc.exp["result"] == "aborted_comm_lost" and svc.phase == "running"
    assert svc.psu.phase not in OUTPUT_PHASES


# ---------------------------------------------------------------------------
# 低 3：檢驗結果作廢與時效
# ---------------------------------------------------------------------------
def test_stale_check_reused_after_failed_recheck(svc):
    """之後一次失敗的檢驗會讓先前成功的結果作廢（驗收重現測試，改為斷言修正後行為）。"""
    res, code = svc.exp_check(body())
    assert code == 200 and res["can_start"]
    res, code = svc.exp_check(dict(body(), label_metal=""))
    assert code == 400
    assert svc._check_result is None
    res, code = svc.exp_start(body(confirm_output=True))
    assert code == 400 and "檢驗 Sensor" in res["errors"][0]


def test_check_expires(svc, monkeypatch):
    res, code = svc.exp_check(body())
    assert code == 200
    monkeypatch.setattr(wm, "CHECK_VALID_S", 0)
    time.sleep(0.05)
    res, code = svc.exp_start(body(confirm_output=True))
    assert code == 400 and "重新「檢驗 Sensor」" in res["errors"][0] and svc.recorder is None


def test_check_must_match_start_params(svc):
    res, code = svc.exp_check(body())
    assert code == 200
    res, code = svc.exp_start(dict(body(confirm_output=True), recipe=dict(RECIPE, u=150)))
    assert code == 400 and "和檢驗時不同" in res["errors"][0] and svc.recorder is None


# ---------------------------------------------------------------------------
# 低 4／5：manifest status 與緊急停止的兩個關閉時間
# ---------------------------------------------------------------------------
def test_estop_manifest_status_and_off_times(svc):
    start(svc)
    assert wait_for(lambda: svc.exp["stage"] == "on", 15)
    rec = svc.recorder
    time.sleep(0.5)
    svc.exp_estop()
    assert wait_for(lambda: svc.exp["output_off_at"] is not None, 8)
    assert svc.exp["output_off_at"] > svc.exp["output_off_cmd_at"]  # 電源回報關閉晚於送出命令
    assert svc.exp_end()
    assert wait_for(lambda: svc.phase == "stopped", 30)
    doc = exp_json(rec)
    assert doc["status"] == "aborted" and doc["result"]["code"] == "aborted_user"
    out = doc["output"]
    assert out["off_command_time"] and out["off_time"] and out["off_command_time"] < out["off_time"]
    assert out["on_s"] > out["timer_s"]


# ---------------------------------------------------------------------------
# 低 6：結束中按緊急停止 → 電源立即關閉、錄製繼續
# ---------------------------------------------------------------------------
def test_estop_during_ending_keeps_recording(svc, monkeypatch):
    orig = svc.psu.power_off

    def hooked():  # 結束流程送出關閉的同時按緊急停止
        orig()
        svc.exp_estop()
    monkeypatch.setattr(svc.psu, "power_off", hooked)
    start(svc, exp_time="1")
    rec = svc.recorder
    assert wait_for(lambda: svc.exp["stage"] == "estop", 15)
    assert wait_for(lambda: svc.psu.phase == "idle", 8)
    time.sleep(1.5)
    assert svc.phase == "running" and svc.exp["stage"] == "estop"  # 錄製沒有被結束流程停掉
    assert svc.exp["result"] == "completed" and "緊急停止" in svc.exp["result_detail"]
    assert svc.exp_end()
    assert wait_for(lambda: svc.phase == "stopped", 30)
    doc = exp_json(rec)
    assert doc["status"] == "completed" and doc["result"]["code"] == "completed"


# ---------------------------------------------------------------------------
# 低 7：Sensor 測試模式被動記錄電源時標記
# ---------------------------------------------------------------------------
def test_sensor_test_marks_passive_psu_logging(svc):
    svc.psu_connect("SIM")
    body_, code = svc.start(dict(wm.DEFAULT_PARAMS), True, mode="sensor_test")
    assert code == 200, body_
    rec = svc.recorder
    time.sleep(1.2)
    svc.request_stop("manual")
    assert wait_for(lambda: svc.phase == "stopped", 30)
    doc = exp_json(rec)
    assert doc["mode"] == "sensor_test" and doc["psu_logging"] == "passive" and doc["status"] == "completed"
