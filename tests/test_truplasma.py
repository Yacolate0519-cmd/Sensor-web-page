"""TruPlasma controller tests against the byte-level simulator. Run: uv run --with pytest pytest tests"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sensors.truplasma_package import controller as ctl
from sensors.truplasma_package import protocol as proto
from sensors.truplasma_package.sim import SimTruPlasma


def wait_for(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(ctl, "RELAYS_SETTLE_S", 0.6)
    sim = SimTruPlasma(seed=1)
    events = []
    c = ctl.TruPlasmaController(lambda: sim, cycle_s=0.1, on_event=lambda lv, m: events.append((lv, m)))
    c.connect()
    assert wait_for(lambda: c.phase == ctl.IDLE), c.error
    yield c, sim, events
    c.disconnect()


def test_frame_matches_logger_verified_crc():
    # Read ch920 as sent on 2026-10-22 by truplasma_logger.py (reply was ACK 4000).
    assert proto.build_frame(proto.CMD_READ_FLOAT, (920).to_bytes(2, "big")).hex() == "0cf3ffff000061420398" + "033c"


def test_parse_real_float_reply():
    reply = bytes.fromhex("12ED00000000400061420398CDCCC0410418")

    class P:
        def __init__(self):
            self.buf = reply

        def reset_input_buffer(self):
            pass

        def write(self, d):
            pass

        def read(self, n):
            out, self.buf = self.buf[:n], self.buf[n:]
            return out

    assert round(proto.read_channel(P(), "float", 920), 1) == 24.1


def test_starts_off_with_rs_control(rig):
    c, sim, _ = rig
    assert wait_for(lambda: c.status and c.status.rs_control)
    assert not sim.relays_on and not sim.power_on


def test_power_on_sequence_and_off(rig):
    c, sim, _ = rig
    wait_for(lambda: c.status.rs_control)
    c.set_setpoints(200, 2, 1)
    t0 = time.monotonic()
    c.power_on()
    assert wait_for(lambda: c.phase == ctl.ON)
    assert time.monotonic() - t0 >= ctl.RELAYS_SETTLE_S
    assert sim.power_on and wait_for(lambda: c.status.u_act > 50)
    c.power_off()
    assert wait_for(lambda: c.phase == ctl.IDLE)
    assert not sim.power_on and not sim.relays_on


def test_refuses_with_zero_setpoints_and_out_of_range(rig):
    c, _, _ = rig
    wait_for(lambda: c.status.rs_control)
    with pytest.raises(RuntimeError):
        c.power_on()
    with pytest.raises(ValueError):
        c.set_setpoints(1001, 1, 1)
    with pytest.raises(ValueError):
        c.set_setpoints(float("nan"), 1, 1)


def test_interlock_blocks_and_drops_output(rig):
    c, sim, events = rig
    wait_for(lambda: c.status.rs_control)
    c.set_setpoints(200, 2, 1)
    sim.interlock = True
    assert wait_for(lambda: c.status.interlock)
    with pytest.raises(RuntimeError):
        c.power_on()
    sim.interlock = False
    assert wait_for(lambda: not c.status.interlock)
    c.power_on()
    assert wait_for(lambda: c.phase == ctl.ON)
    sim.interlock = True
    sim._trip(61709)
    assert wait_for(lambda: c.phase == ctl.IDLE)
    assert not c.want_power and not c.want_relays
    # Clearing the fault must not switch the output back on by itself.
    sim.interlock = False
    sim.alarm = 0
    time.sleep(0.5)
    assert not sim.power_on


def test_emergency_stop(rig):
    c, sim, _ = rig
    wait_for(lambda: c.status.rs_control)
    c.set_setpoints(200, 2, 1)
    c.power_on()
    assert wait_for(lambda: c.phase == ctl.ON)
    c.emergency_stop()
    assert wait_for(lambda: not sim.power_on and not sim.relays_on, 1.0)


def test_disconnect_switches_off(rig):
    c, sim, _ = rig
    wait_for(lambda: c.status.rs_control)
    c.set_setpoints(200, 2, 1)
    c.power_on()
    assert wait_for(lambda: c.phase == ctl.ON)
    c.disconnect()
    assert not sim.power_on and c.phase == ctl.DISCONNECTED


def test_heartbeat_loss_trips_watchdog(monkeypatch):
    monkeypatch.setattr(ctl, "RELAYS_SETTLE_S", 0.2)
    sim = SimTruPlasma(seed=2)
    monkeypatch.setattr("sensors.truplasma_package.sim.WATCHDOG_S", 0.5)
    c = ctl.TruPlasmaController(lambda: sim, cycle_s=0.1)
    c.connect()
    assert wait_for(lambda: c.phase == ctl.IDLE)
    wait_for(lambda: c.status.rs_control)
    c.set_setpoints(200, 2, 1)
    c.power_on()
    assert wait_for(lambda: c.phase == ctl.ON)
    # Simulate a frozen program: the worker stops talking without the clean-shutdown frames.
    c.stop_event.set()
    sim.write = lambda data: len(data)
    time.sleep(0.8)
    sim._tick()
    assert sim.alarm == 61613 and not sim.power_on


def test_param_write_readback_and_lockout(rig):
    c, sim, _ = rig
    wait_for(lambda: c.status.rs_control)
    params = c.read_params()
    assert params[51131] == 20.0
    res = c.write_param(51131, 30)
    assert res["new"] == 30 and sim.params[51131] == 30
    with pytest.raises(ValueError):
        c.write_param(28, 0)          # communication: not writable
    with pytest.raises(ValueError):
        c.write_param(51131, 150)     # out of manual range
    with pytest.raises(ValueError):
        c.write_param(33, 3)          # two modes at once
    c.set_setpoints(200, 2, 1)
    c.power_on()
    assert wait_for(lambda: c.phase == ctl.ON)
    with pytest.raises(RuntimeError):
        c.write_param(51131, 40)


def test_alarm_reported_and_reset(rig):
    c, sim, events = rig
    sim._trip(61623)
    assert wait_for(lambda: c.alarm and c.alarm["code"] == 61623)
    c.reset_alarms()
    assert wait_for(lambda: not c.status.alarm_active)
    assert any("61623" in m for _, m in events)
