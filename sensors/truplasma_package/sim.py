"""Byte-level TruPlasma simulator: a serial-port stand-in that answers the real protocol.

Models the behaviour the controller depends on: edge-sensitive Ctrl bits, relays settle time,
interlock, alarms, the 4-5 s ActSourceFail watchdog and EEPROM range checks.
"""
import random
import struct
import threading
import time

from . import protocol as proto
from .params import I_NOM, P_NOM, PARAMS_BY_CH, U_NOM

# Values read from the real unit on 2026-10-22.
INITIAL = {
    33: 2, 51130: 1.0, 51131: 20.0, 650: 80, 140: 1, 1100: 500, 652: 10, 653: 10, 654: 10, 655: 10,
    803: 0, 200: 6, 209: 1, 202: 0, 205: 32.5, 207: 25.0, 208: 100.0, 250: 100.0, 251: 50.0, 256: 80.0,
    252: 20, 261: 0, 265: 3, 270: 0, 275: 1000, 276: 120, 1512: 0, 451: 0, 454: 0, 450: 2.5, 453: 800.0,
    18: 171, 19: 1, 20: 1, 28: 16, 3: 0, 5: 3, 120: 0, 1200: 1, 65461: 100.0, 65455: 25.0,
}
LOAD_OHM = 40.0
RELAYS_DELAY_S = 0.5
WATCHDOG_S = 4.5


class SimTruPlasma:
    def __init__(self, faults=False, seed=None):
        self.faults = faults
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.params = dict(INITIAL)
        self.out = b""
        self.last_ctrl = 0
        self.last_cmd_t = time.monotonic()
        self.rs_control = False
        self.relays_cmd_t = None
        self.relays_on = False
        self.power_on = False
        self.interlock = False
        self.alarm = 0
        self.u = self.i = self.p = 0.0
        self.counts = [0] * 5
        self.closed = False

    # serial-like interface
    timeout = 0.3

    def reset_input_buffer(self):
        self.out = b""

    def close(self):
        self.closed = True

    def write(self, data):
        with self.lock:
            self._tick()
            if self.faults and self.rng.random() < 0.02:
                return len(data)  # dropped frame → controller sees a timeout
            self.out = self._handle(bytes(data))
        return len(data)

    def read(self, n):
        with self.lock:
            chunk, self.out = self.out[:n], self.out[n:]
        if len(chunk) < n:
            time.sleep(self.timeout)
        return chunk

    # protocol
    def _reply(self, ack, cmd, data=b""):
        body = bytes([0, 0, 0, 0, ack >> 8, ack & 0xFF, cmd >> 8, cmd & 0xFF]) + data
        length = len(body) + 4
        crc = proto.checksum(body)
        return bytes([length, 0xFF ^ length]) + body + bytes([crc >> 8, crc & 0xFF])

    def _handle(self, frame):
        if len(frame) < 10 or frame[0] != len(frame) or frame[1] != 0xFF ^ frame[0]:
            return self._reply(0x4001, 0)
        if proto.checksum(frame[2:-2]) != (frame[-2] << 8 | frame[-1]):
            return self._reply(0x4002, 0)
        cmd = frame[6] << 8 | frame[7]
        data = frame[8:-2]
        self.last_cmd_t = time.monotonic()
        if cmd == proto.CMD_RUN:
            return self._run(data)
        if cmd == proto.CMD_IDENT:
            return self._reply(proto.ACK_OK, 0x7701, b"SIM-4010-G21\x00")
        if cmd in (proto.CMD_READ_ALARM, proto.CMD_READ_LAST_ALARM):
            text = proto.ALARM_TEXT.get(self.alarm, "").encode("ascii", "replace") if self.alarm else b""
            return self._reply(proto.ACK_OK, cmd, self.alarm.to_bytes(2, "big") + text + b"\x00")
        kinds = {v: k for k, v in proto.READ_CMDS.items()}
        set_kinds = {v: k for k, v in proto.SET_CMDS.items()}
        if cmd in kinds:
            ch = data[0] << 8 | data[1]
            value = self._measure(ch)
            if value is None:
                return self._reply(0x4006, cmd)
            return self._reply(proto.ACK_OK, cmd, data[:2] + proto.encode_value(kinds[cmd], value))
        if cmd in set_kinds:
            ch = data[0] << 8 | data[1]
            p = PARAMS_BY_CH.get(ch)
            if p is None or ch not in self.params:
                return self._reply(0x4006, cmd)
            value = proto.decode_value(set_kinds[cmd], data[2:])
            if p.lo is not None and value < p.lo:
                return self._reply(0x4032, cmd)
            if p.hi is not None and value > p.hi:
                return self._reply(0x4031, cmd)
            self.params[ch] = value
            return self._reply(proto.ACK_OK, cmd)
        return self._reply(0x4004, cmd)

    def _run(self, data):
        u_set, i_set, p_set = struct.unpack("<fff", data[:12])
        ctrl = data[12]
        rising = ctrl & ~self.last_ctrl
        self.last_ctrl = ctrl
        if rising & proto.CTRL_RS_CONTROL:
            self.rs_control = True
        if ctrl & proto.CTRL_RESET_ALARMS and self.alarm and not self.interlock:
            self.alarm = 0
        if ctrl & proto.CTRL_RESET_COUNTERS:
            self.counts = [0] * 5
        if self.rs_control:
            if not ctrl & proto.CTRL_RELAYS:
                self.relays_on = self.power_on = False
                self.relays_cmd_t = None
            elif rising & proto.CTRL_RELAYS and not self.alarm and not self.interlock:
                self.relays_cmd_t = time.monotonic()
            if not ctrl & proto.CTRL_POWER:
                self.power_on = False
            elif rising & proto.CTRL_POWER and self.relays_on and not self.alarm and not self.interlock:
                self.power_on = True
        self._tick()
        self._output(min(u_set, U_NOM), min(i_set, I_NOM), min(p_set, P_NOM))
        b0 = (self.relays_on << 0) | (self.power_on << 1) | ((not self.alarm) << 2) | (self.rs_control << 3)
        b0 |= (self.power_on and self.u > 50) << 7
        b1 = self.params[33]
        b2 = (self.interlock << 0) | ((self.alarm != 0) << 7)
        b3 = (self.params[140] << 5)
        reply = struct.pack("<fff", self.u, self.i * 1.6, self.p) + bytes([b0, b1, b2, b3])
        reply += struct.pack(">5H", *self.counts) + struct.pack("<f", self._arc_rate())
        return self._reply(proto.ACK_OK, proto.CMD_RUN, reply)

    def _tick(self):
        now = time.monotonic()
        if self.relays_cmd_t is not None and not self.relays_on and now - self.relays_cmd_t >= RELAYS_DELAY_S:
            self.relays_on = True
        if self.rs_control and now - self.last_cmd_t > WATCHDOG_S and not self.alarm:
            self._trip(61613)
        if self.faults and self.power_on and self.rng.random() < 0.002:
            self._trip(61625)

    def _trip(self, code):
        self.alarm = code
        self.relays_on = self.power_on = False
        self.relays_cmd_t = None

    def _output(self, u_set, i_set, p_set):
        if not self.power_on:
            self.u = self.i = self.p = 0.0
            return
        # Resistive load: the first limit reached (U, I or P) sets the operating point.
        u = min(u_set, i_set * LOAD_OHM, (p_set * 1000 * LOAD_OHM) ** 0.5)
        target = u * (1 + self.rng.gauss(0, 0.01))
        self.u += (target - self.u) * 0.5
        self.i = self.u / LOAD_OHM
        self.p = self.u * self.i / 1000
        if self.rng.random() < 0.05:
            self.counts[self.rng.randrange(3)] += 1

    def _arc_rate(self):
        return self.rng.uniform(0, 3) if self.power_on else 0.0

    def _measure(self, ch):
        if ch in self.params:
            return self.params[ch]
        mode, duty = self.params[33], self.params[51131] / 100
        a = duty if mode == 32 else (1 if mode == 4 else 0)
        b = 1 - duty if mode == 32 else (1 if mode == 8 else 0)
        freq = self.params[1100] if self.params[140] and mode in (4, 8, 16) else self.params[51130] * 1000
        live = {966: self.u * a, 967: self.i * a, 980: self.p * a,
                976: self.u * b, 977: self.i * b, 981: self.p * b,
                932: 27.0 + self.p * 2, 51140: freq, 51141: self.params[51131]}
        return live.get(ch)
