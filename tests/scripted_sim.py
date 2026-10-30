"""Fault injection for tests: scripted variants of the built-in simulators.

The built-in --simulate devices cannot produce rangefinder RC errors or PSU warnings/messages
(61657, 61711, 61714); these subclasses add that without touching the real hardware path.
Only the simulators are used here: no DLL, no COM port.
"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "sensors", ROOT / "app"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from rangefinder.core import LKIFError  # noqa: E402
from truplasma_package import protocol as proto  # noqa: E402
from truplasma_package.sim import SimTruPlasma  # noqa: E402

import sim_devices  # noqa: E402


class ScriptedPsu(SimTruPlasma):
    """SimTruPlasma plus a settable 'current message' (code, text, kind).

    kind="warning" sets Bits2.b6 (warning active) only; kind="alarm" sets Bits2.b7 like a real alarm.
    The message is what CMD_READ_ALARM returns, so the controller sees it exactly like the unit's own text.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.msg = None  # (code, text, kind)

    def set_message(self, code, text, kind="warning"):
        with self.lock:
            self.msg = (code, text, kind) if code else None

    def _handle(self, frame):
        cmd = frame[6] << 8 | frame[7] if len(frame) >= 8 else 0
        if cmd in (proto.CMD_READ_ALARM, proto.CMD_READ_LAST_ALARM) and self.msg:
            code, text, _ = self.msg
            return self._reply(proto.ACK_OK, cmd, code.to_bytes(2, "big") + text.encode("latin-1") + b"\x00")
        return super()._handle(frame)

    def _run(self, data):
        reply = super()._run(data)
        if not self.msg:
            return reply
        payload = bytearray(reply[10:-2])
        payload[14] |= 0x80 if self.msg[2] == "alarm" else 0x40  # Bits2: b7 alarm, b6 warning
        return self._reply(proto.ACK_OK, proto.CMD_RUN, bytes(payload))


class ScriptedRangefinder(sim_devices.SimLKIF2Device):
    """SimLKIF2Device whose read_single follows a script of (seconds, outcome) steps, then repeats the last.

    outcome: "VALID", a FloatResult name ("ALARM", "WAITING", ...), or an int RC code → LKIFError.
    The script can be swapped while running (set_script) to drive a scenario step by step.
    """

    script = [(1e9, "VALID")]
    instances = []

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._lock = threading.Lock()
        self._steps = list(type(self).script)
        self._t_step = time.time()
        type(self).instances.append(self)

    def set_script(self, steps):
        with self._lock:
            self._steps = list(steps)
            self._t_step = time.time()

    def _outcome(self):
        with self._lock:
            t = time.time() - self._t_step
            for dur, outcome in self._steps:
                if t < dur:
                    return outcome
                t -= dur
            return self._steps[-1][1]

    def read_single(self, out_no=0):
        outcome = self._outcome()
        if isinstance(outcome, int):
            raise LKIFError("Read failed", outcome)
        if outcome == "VALID":
            return super().read_single(out_no)
        code = {"+RANGEOVER": 1, "-RANGEOVER": 2, "WAITING": 3, "ALARM": 4, "INVALID": 5}.get(outcome, 9)
        return {"OutNo": out_no, "RawStatus": code, "FloatResult": outcome, "Value": 0.0}
