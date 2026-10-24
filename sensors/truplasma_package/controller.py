"""TruPlasma controller: owns the serial port, sends the 6040 heartbeat, sequences output on/off.

All serial traffic happens on one worker thread. Other threads only change the desired state
or queue jobs, so nothing else ever touches the port.

Safety rules (manual §9.1 and p.47/p.148):
- 6040 is sent every `cycle_s` (default 0.5 s). If this program stops, the unit raises
  61613 ActSourceFail after 4-5 s and switches the output off by itself.
- Output always starts OFF. Any alarm, interlock, loss of the Power ON ack or 3 failed
  heartbeats in a row drops the request back to OFF; the user must switch on again.
- Switch-on order: Ready, no alarm, no interlock → Mains relays → wait for the relays ack and
  at least 3 s → Power ON.
- Parameters other than U/I/P setpoints are written to EEPROM, so they may only be changed
  while the output is off, and each write is read back.
"""
import logging
import queue
import threading
import time
from concurrent.futures import Future

from . import protocol as proto
from .params import MEASUREMENTS, PARAMS, PARAMS_BY_CH, check_setpoints, check_value

log = logging.getLogger("sensor_monitor")

RELAYS_SETTLE_S = 3.0
RELAYS_TIMEOUT_S = 10.0
POWER_TIMEOUT_S = 5.0
OFF_TIMEOUT_S = 5.0
MAX_HEARTBEAT_FAILS = 3
JOB_BUDGET_S = 0.25

# phase values
DISCONNECTED, CONNECTING, IDLE, RELAYS, POWERING, ON, STOPPING = (
    "disconnected", "connecting", "idle", "relays", "powering", "on", "stopping")
PHASE_TEXT = {
    DISCONNECTED: "未連線", CONNECTING: "連線中", IDLE: "輸出關閉", RELAYS: "主繼電器接通中",
    POWERING: "開啟輸出中", ON: "輸出中", STOPPING: "關閉輸出中",
}


def open_serial(port, baudrate=115200):
    import serial
    s = serial.Serial(port, baudrate, timeout=0.3, write_timeout=0.5, dsrdtr=False, rtscts=False)
    s.dtr = False
    s.rts = False
    return s


class TruPlasmaController:
    def __init__(self, port_factory, address=0xFFFF, cycle_s=0.5, on_cycle=None, on_event=None):
        self.port_factory = port_factory
        self.address = address
        self.cycle_s = cycle_s
        self.on_cycle = on_cycle      # called with snapshot() after every heartbeat
        self.on_event = on_event      # called with (level, message) for notable events
        self.lock = threading.RLock()
        self.jobs = queue.Queue()
        self.thread = None
        self.stop_event = threading.Event()
        self._reset_state()

    def _reset_state(self):
        self.phase = DISCONNECTED
        self.setpoints = (0.0, 0.0, 0.0)
        self.want_relays = False
        self.want_power = False
        self.pulse_flags = 0
        self.rs_taken = False
        self.phase_since = time.monotonic()
        self.status = None
        self.measurements = {}
        self.alarm = None
        self.identity = ""
        self.error = ""
        self.fail_count = 0
        self.last_ok = None
        self.params = {}

    # ----- public API (any thread) -----
    def connect(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self._reset_state()
            self.phase = CONNECTING
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self._worker, name="truplasma", daemon=True)
            self.thread.start()

    def disconnect(self, timeout=3.0):
        """Switch the output off and release the port."""
        t = self.thread
        self.stop_event.set()
        if t:
            t.join(timeout)

    @property
    def connected(self):
        return self.phase not in (DISCONNECTED, CONNECTING)

    def set_setpoints(self, u, i, p):
        sp = check_setpoints(u, i, p)
        with self.lock:
            if not self.connected:
                raise RuntimeError("電源未連線")
            self.setpoints = sp
        return sp

    def power_on(self):
        with self.lock:
            st = self.status
            if self.phase != IDLE or st is None:
                raise RuntimeError(f"目前狀態「{PHASE_TEXT[self.phase]}」不能開啟輸出")
            if st.alarm_active:
                raise RuntimeError("電源有警報，請先排除原因並重設警報")
            if st.interlock:
                raise RuntimeError("Interlock 觸發中（上蓋開關或類比介面 pin 12），不能開啟輸出")
            if not st.ready:
                raise RuntimeError("電源未就緒（Ready = 0）")
            if not st.rs_control:
                raise RuntimeError("程式沒有取得 RS232 控制權，請確認面板上的控制來源")
            if not any(self.setpoints):
                raise RuntimeError("電壓、電流、功率設定值都是 0")
            self.want_relays = True
            self._set_phase(RELAYS)
        self._event("info", "開始開啟輸出：接通主繼電器")

    def power_off(self):
        with self.lock:
            if self.phase not in (RELAYS, POWERING, ON):
                return
            self.want_power = False
            self._set_phase(STOPPING)
        self._event("info", "關閉輸出")

    def emergency_stop(self):
        """Drop power and relays in the same frame; phase becomes idle once the unit confirms."""
        with self.lock:
            active = self.want_power or self.want_relays
            self.want_power = False
            self.want_relays = False
            if not active:
                return
            self._set_phase(STOPPING)
        self._event("warning", "緊急關閉輸出")

    def reset_alarms(self):
        with self.lock:
            if not self.connected:
                raise RuntimeError("電源未連線")
            if self.phase != IDLE:
                raise RuntimeError("請先關閉輸出再重設警報")
            self.pulse_flags |= proto.CTRL_RESET_ALARMS

    def reset_counters(self):
        with self.lock:
            if not self.connected:
                raise RuntimeError("電源未連線")
            self.pulse_flags |= proto.CTRL_RESET_COUNTERS

    def read_params(self, timeout=10.0):
        return self._submit(self._job_read_params).result(timeout)

    def write_param(self, channel, value, timeout=5.0):
        param = PARAMS_BY_CH.get(int(channel))
        if param is None:
            raise ValueError(f"ch{channel} 不在可設定清單")
        v = check_value(param, value)
        with self.lock:
            if self.phase != IDLE:
                raise RuntimeError("參數會寫入電源的 EEPROM，請先關閉輸出再修改")
        return self._submit(lambda port: self._job_write_param(port, param, v)).result(timeout)

    def snapshot(self):
        with self.lock:
            st = self.status
            return {
                "phase": self.phase,
                "phase_text": PHASE_TEXT[self.phase],
                "connected": self.connected,
                "setpoints": {"u": self.setpoints[0], "i": self.setpoints[1], "p": self.setpoints[2]},
                "status": st.to_dict() if st else None,
                "measurements": dict(self.measurements),
                "alarm": self.alarm,
                "identity": self.identity,
                "error": self.error,
                "fail_count": self.fail_count,
                "last_ok": self.last_ok,
            }

    # ----- internals -----
    def _event(self, level, msg):
        log.log(logging.WARNING if level in ("warning", "error") else logging.INFO, "TruPlasma: %s", msg)
        if self.on_event:
            try:
                self.on_event(level, msg)
            except Exception:
                log.exception("TruPlasma on_event callback failed")

    def _set_phase(self, phase):
        self.phase = phase
        self.phase_since = time.monotonic()

    def _submit(self, fn):
        if not (self.thread and self.thread.is_alive()):
            raise RuntimeError("電源未連線")
        fut = Future()
        self.jobs.put((fn, fut))
        return fut

    def _drop_output(self, reason, level="warning"):
        """Request OFF after a fault. Caller holds the lock."""
        was = self.want_relays or self.want_power
        self.want_relays = False
        self.want_power = False
        self._set_phase(IDLE)
        if was:
            self.error = reason
            return level, reason
        return None

    def _ctrl_byte(self):
        ctrl = proto.CTRL_RS_CONTROL if self.rs_taken else 0
        if self.want_relays:
            ctrl |= proto.CTRL_RELAYS
        if self.want_power:
            ctrl |= proto.CTRL_POWER
        return ctrl | self.pulse_flags

    def _heartbeat(self, port):
        with self.lock:
            u, i, p = self.setpoints
            ctrl = self._ctrl_byte()
            sent_flags = self.pulse_flags
        st = proto.run(port, u, i, p, ctrl, self.address)
        with self.lock:
            self.pulse_flags &= ~sent_flags
            # First frame goes out with b3 = 0 so the next one is a 0→1 edge (b3 is edge sensitive).
            self.rs_taken = True
            self.status = st
            self.fail_count = 0
            self.last_ok = time.time()
            return self._sequence(st)

    def _sequence(self, st):
        """Advance the on/off state machine. Caller holds the lock. Returns (level, message) or None."""
        now = time.monotonic()
        age = now - self.phase_since
        if self.phase in (RELAYS, POWERING, ON):
            if st.alarm_active:
                return self._drop_output("電源警報，輸出已關閉")
            if st.interlock:
                return self._drop_output("Interlock 觸發，輸出已關閉")
        if self.phase == RELAYS:
            if st.relays_on and age >= RELAYS_SETTLE_S:
                self.want_power = True
                self._set_phase(POWERING)
                return "info", "主繼電器已接通，開啟輸出"
            if age > RELAYS_TIMEOUT_S:
                return self._drop_output(f"{RELAYS_TIMEOUT_S:g} 秒內主繼電器沒有接通，已取消")
        elif self.phase == POWERING:
            if st.power_on:
                self._set_phase(ON)
                return "info", "輸出已開啟"
            if age > POWER_TIMEOUT_S:
                return self._drop_output(f"{POWER_TIMEOUT_S:g} 秒內電源沒有回報輸出開啟，已取消")
        elif self.phase == ON:
            if not st.power_on and age > 2 * self.cycle_s:
                return self._drop_output("電源回報輸出已關閉（INHIBIT），已停止")
        elif self.phase == STOPPING:
            if not st.power_on:
                # Power first, then relays; done once the unit reports both off.
                self.want_relays = False
                if not st.relays_on:
                    self._set_phase(IDLE)
                    return "info", "輸出已關閉"
            if age > OFF_TIMEOUT_S:
                self.want_relays = False
                self._set_phase(IDLE)
                return "warning", "關閉輸出逾時，已同時切斷主繼電器"
        return None

    def _poll_extras(self, port):
        values = {}
        for ch, name in MEASUREMENTS:
            try:
                values[name] = round(proto.read_channel(port, "float", ch, self.address), 4)
            except proto.ProtocolError:
                values[name] = None
        alarm = None
        st = self.status
        if st and (st.alarm_active or st.warning_active):
            try:
                code, text = proto.read_alarm(port, self.address)
                if code:
                    alarm = {"code": code, "text": proto.ALARM_TEXT.get(code, text), "raw": text,
                             "level": "alarm" if st.alarm_active else "warning"}
            except proto.ProtocolError:
                alarm = self.alarm
        with self.lock:
            changed = (alarm or {}).get("code") != (self.alarm or {}).get("code")
            self.measurements = values
            self.alarm = alarm
        if changed and alarm:
            self._event("error" if alarm["level"] == "alarm" else "warning",
                        f"電源{'警報' if alarm['level'] == 'alarm' else '警告'} {alarm['code']}：{alarm['text']}")

    def _job_read_params(self, port):
        out = {}
        for p in PARAMS:
            try:
                out[p.channel] = proto.read_channel(port, p.kind, p.channel, self.address)
            except proto.ProtocolError as e:
                out[p.channel] = None
                log.warning("TruPlasma: 讀取 ch%s 失敗：%s", p.channel, e)
            self._heartbeat_if_due(port)
        with self.lock:
            self.params = out
        return out

    def _job_write_param(self, port, param, value):
        with self.lock:
            if self.phase != IDLE:
                raise RuntimeError("輸出已開啟，取消寫入")
        old = proto.read_channel(port, param.kind, param.channel, self.address)
        proto.write_channel(port, param.kind, param.channel, value, self.address)
        new = proto.read_channel(port, param.kind, param.channel, self.address)
        ok = abs(new - value) < 1e-3 * max(1.0, abs(value)) if param.kind == "float" else new == value
        with self.lock:
            self.params[param.channel] = new
        msg = f"{param.name}（ch{param.channel}）{old:g} → {new:g} {param.unit}".rstrip()
        self._event("info" if ok else "warning", ("已寫入 " if ok else "寫入後讀回的值不一致：") + msg)
        if not ok:
            raise RuntimeError(f"寫入後讀回 {new:g}，跟要寫入的 {value:g} 不同")
        return {"channel": param.channel, "old": old, "new": new}

    def _heartbeat_if_due(self, port):
        if time.monotonic() - self._last_beat >= self.cycle_s:
            self._beat(port)

    def _beat(self, port):
        self._last_beat = time.monotonic()
        try:
            msg = self._heartbeat(port)
        except proto.ProtocolError as e:
            with self.lock:
                self.fail_count += 1
                self.error = str(e)
                msg = None
                if self.fail_count >= MAX_HEARTBEAT_FAILS:
                    msg = self._drop_output(f"連續 {self.fail_count} 次沒有收到電源回應，輸出已要求關閉", "error")
            if msg:
                self._event(*msg)
            return False
        if msg:
            self._event(*msg)
        return True

    def _run_jobs(self, port, deadline):
        while time.monotonic() < deadline:
            try:
                fn, fut = self.jobs.get_nowait()
            except queue.Empty:
                return
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(port))
            except BaseException as e:
                fut.set_exception(e)

    def _send_off(self, port):
        with self.lock:
            self.want_power = False
            self.want_relays = False
            self.pulse_flags = 0
        for _ in range(2):
            try:
                self._heartbeat(port)
            except Exception:
                pass

    def _fail_jobs(self, exc):
        while True:
            try:
                _, fut = self.jobs.get_nowait()
            except queue.Empty:
                return
            if fut.set_running_or_notify_cancel():
                fut.set_exception(exc)

    def _worker(self):
        port = None
        try:
            port = self.port_factory()
            try:
                ident = proto.transact(port, proto.CMD_IDENT, b"", self.address)
                ident_text = ident.split(b"\x00")[0].decode("latin-1", "replace").strip()
            except proto.ProtocolError as e:
                raise proto.ProtocolError(f"電源沒有正確回應識別命令：{e}") from None
            with self.lock:
                self.identity = ident_text
            self._last_beat = 0.0
            if not self._beat(port):
                raise proto.ProtocolError(self.error)
            with self.lock:
                self.error = ""
                self._set_phase(IDLE)
            self._event("info", f"已連線（{ident_text}），輸出關閉")
            while not self.stop_event.is_set():
                t0 = time.monotonic()
                self._beat(port)
                if self.status:
                    self._poll_extras(port)
                if self.on_cycle:
                    try:
                        self.on_cycle(self.snapshot())
                    except Exception:
                        log.exception("TruPlasma on_cycle callback failed")
                self._run_jobs(port, time.monotonic() + JOB_BUDGET_S)
                self.stop_event.wait(max(0.0, self.cycle_s - (time.monotonic() - t0)))
        except Exception as e:
            with self.lock:
                self.error = str(e)
            self._event("error", f"電源連線中斷：{e}")
        finally:
            if port is not None:
                self._send_off(port)
                try:
                    port.close()
                except Exception:
                    pass
            with self.lock:
                self.want_power = self.want_relays = False
                self._set_phase(DISCONNECTED)
            self._fail_jobs(RuntimeError("電源連線已關閉"))
            if self.on_cycle:
                try:
                    self.on_cycle(self.snapshot())
                except Exception:
                    pass
