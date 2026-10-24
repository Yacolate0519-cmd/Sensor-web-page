"""TruPlasma Bipolar 4000 series RS232 framing (manual §9.1, p.67-79).

Command: LEN ~LEN DSTH DSTL SRCH SRCL CMDH CMDL data... CRCH CRCL
Reply:   LEN ~LEN DSTH DSTL SRCH SRCL ACKH ACKL CMDH CMDL data... CRCH CRCL
CRC = 16-bit sum of bytes 2..LEN-3, big-endian.
Verified on the real unit 2026-10-22: floats are little-endian, integers big-endian.
"""
import struct
from dataclasses import dataclass, field

CMD_RUN = 0x6040
CMD_IDENT = 0x6101
CMD_SET_BYTE, CMD_READ_BYTE = 0x6111, 0x6112
CMD_SET_INT, CMD_READ_INT = 0x6121, 0x6122
CMD_SET_FLOAT, CMD_READ_FLOAT = 0x6141, 0x6142
CMD_SET_DINT, CMD_READ_DINT = 0x6151, 0x6152
CMD_READ_ALARM, CMD_READ_LAST_ALARM = 0x6301, 0x6302

READ_CMDS = {"byte": CMD_READ_BYTE, "int": CMD_READ_INT, "float": CMD_READ_FLOAT, "dint": CMD_READ_DINT}
SET_CMDS = {"byte": CMD_SET_BYTE, "int": CMD_SET_INT, "float": CMD_SET_FLOAT, "dint": CMD_SET_DINT}

ACK_OK = 0x4000
ACK_TEXT = {
    0x4001: "長度錯誤",
    0x4002: "檢查碼錯誤",
    0x4004: "不支援的命令",
    0x4006: "channel 不存在",
    0x4010: "寫入 EEPROM 失敗",
    0x4020: "從機模式禁止寫入",
    0x4030: "禁止寫入 EEPROM",
    0x4031: "超過上限",
    0x4032: "低於下限",
}

# 6040 Ctrl byte
CTRL_RELAYS = 0x01
CTRL_POWER = 0x02
CTRL_RESET_COUNTERS = 0x04
CTRL_RS_CONTROL = 0x08
CTRL_RESET_ALARMS = 0x80

PULSE_MODES = ["DC-A", "DC-B", "MonoPulse-A", "MonoPulse-B", "Full", "BiPulse", "Trapez", "Step"]

ALARM_TEXT = {
    61601: "EEPROM 錯誤", 61603: "FPGA 錯誤", 61605: "24 V 供電異常", 61606: "24 V 供電異常",
    61607: "過溫", 61609: "市電電壓下降", 61610: "高壓電源通訊異常", 61611: "介面通訊異常",
    61613: "控制來源中斷（ActSourceFail，超過 4–5 秒沒收到命令）", 61616: "U500 異常", 61617: "U500 異常",
    61618: "Inverter 異常", 61619: "U800 異常", 61620: "U800 異常", 61621: "U800 異常",
    61623: "無負載", 61624: "短路", 61625: "電弧密度過高", 61631: "冷卻水進水溫度異常",
    61694: "PFC 緊急停止", 61695: "反向過電流", 61709: "輸出緊急停止", 61710: "風扇關閉過久",
    61654: "電弧過多", 61657: "過溫警告", 61658: "冷卻水流量過低", 61659: "冷卻水流向錯誤",
    61661: "介面通訊中斷", 61663: "目前控制來源通訊中斷", 61665: "設定值與實際值偏差過大",
    61666: "無法起弧（No Plasma）", 61670: "冷卻水進水溫度過低",
}


class ProtocolError(IOError):
    pass


class AckError(ProtocolError):
    def __init__(self, ack, cmd, channel=None):
        self.ack = ack
        where = f"ch {channel}" if channel is not None else f"cmd {cmd:04X}"
        super().__init__(f"{where}: 電源回應 {ack:04X} {ACK_TEXT.get(ack, '')}".strip())


def checksum(body):
    return sum(body) & 0xFFFF


def build_frame(cmd, data=b"", address=0xFFFF):
    body = bytes([address >> 8 & 0xFF, address & 0xFF, 0, 0, cmd >> 8, cmd & 0xFF]) + bytes(data)
    length = len(body) + 4
    crc = checksum(body)
    return bytes([length, 0xFF ^ length]) + body + bytes([crc >> 8, crc & 0xFF])


def read_reply(port):
    """Read one reply frame from a serial-like object; return (ack, cmd, data)."""
    head = port.read(2)
    if len(head) < 2:
        raise ProtocolError("電源沒有回應（逾時）")
    if head[1] != 0xFF ^ head[0] or head[0] < 12:
        port.reset_input_buffer()
        raise ProtocolError(f"回應開頭錯誤 {head.hex(' ')}")
    rest = port.read(head[0] - 2)
    frame = head + rest
    if len(frame) != head[0]:
        raise ProtocolError(f"回應不完整 {frame.hex(' ')}")
    if checksum(frame[2:-2]) != (frame[-2] << 8 | frame[-1]):
        raise ProtocolError(f"回應檢查碼錯誤 {frame.hex(' ')}")
    ack = frame[6] << 8 | frame[7]
    cmd = frame[8] << 8 | frame[9]
    return ack, cmd, frame[10:-2]


def transact(port, cmd, data=b"", address=0xFFFF, channel=None):
    port.reset_input_buffer()
    port.write(build_frame(cmd, data, address))
    ack, _, payload = read_reply(port)
    if ack != ACK_OK:
        raise AckError(ack, cmd, channel)
    return payload


def encode_value(kind, value):
    if kind == "byte":
        return bytes([int(value) & 0xFF])
    if kind == "int":
        return int(value).to_bytes(2, "big")
    if kind == "dint":
        return int(value).to_bytes(4, "big")
    if kind == "float":
        return struct.pack("<f", float(value))
    raise ValueError(kind)


def decode_value(kind, data):
    if kind == "byte":
        return data[0]
    if kind in ("int", "dint"):
        return int.from_bytes(data, "big")
    if kind == "float":
        return struct.unpack("<f", data[:4])[0]
    raise ValueError(kind)


def read_channel(port, kind, channel, address=0xFFFF):
    payload = transact(port, READ_CMDS[kind], channel.to_bytes(2, "big"), address, channel)
    # Reply data repeats the channel number before the value.
    return decode_value(kind, payload[2:])


def write_channel(port, kind, channel, value, address=0xFFFF):
    transact(port, SET_CMDS[kind], channel.to_bytes(2, "big") + encode_value(kind, value), address, channel)


def read_alarm(port, address=0xFFFF, last=False):
    payload = transact(port, CMD_READ_LAST_ALARM if last else CMD_READ_ALARM, b"", address)
    if len(payload) < 2:
        return None, ""
    code = payload[0] << 8 | payload[1]
    text = payload[2:].split(b"\x00")[0].decode("latin-1", "replace").strip()
    return code, text


def bit(value, n):
    return bool(value >> n & 1)


@dataclass
class RunStatus:
    """Decoded 6040 reply."""
    u_act: float
    i_act_peak: float
    p_act: float
    bits0: int
    bits1: int
    bits2: int
    bits3: int
    arc_counts: dict = field(default_factory=dict)
    arc_rate: float = 0.0

    @property
    def relays_on(self):
        return bit(self.bits0, 0)

    @property
    def power_on(self):
        return bit(self.bits0, 1)

    @property
    def ready(self):
        return bit(self.bits0, 2)

    @property
    def rs_control(self):
        return bit(self.bits0, 3)

    @property
    def interlock(self):
        return bit(self.bits2, 0)

    @property
    def fpga_error(self):
        return bit(self.bits2, 3)

    @property
    def power_off_sequence(self):
        return bit(self.bits2, 4)

    @property
    def warning_active(self):
        return bit(self.bits2, 6)

    @property
    def alarm_active(self):
        return bit(self.bits2, 7)

    @property
    def mode(self):
        names = [PULSE_MODES[n] for n in range(8) if bit(self.bits1, n)]
        return "/".join(names) or "-"

    @property
    def regulation(self):
        names = [name for n, name in ((0, "U"), (1, "I"), (2, "P")) if bit(self.bits3, n)]
        return "/".join(names) or "-"

    def to_dict(self):
        return {
            "u_act": round(self.u_act, 2), "i_act_peak": round(self.i_act_peak, 3), "p_act": round(self.p_act, 4),
            "bits": [self.bits0, self.bits1, self.bits2, self.bits3],
            "relays_on": self.relays_on, "power_on": self.power_on, "ready": self.ready,
            "rs_control": self.rs_control, "interlock": self.interlock, "fpga_error": self.fpga_error,
            "power_off_sequence": self.power_off_sequence, "warning_active": self.warning_active,
            "alarm_active": self.alarm_active, "mode": self.mode, "regulation": self.regulation,
            "low_freq": bit(self.bits3, 5), "arc_occurred": bit(self.bits3, 7),
            # Manual says Bits0.b7 is PlasmaBit; the 2020 PVDPower dump puts it at b6. Unverified, display only.
            "plasma_bit_b6": bit(self.bits0, 6), "plasma_bit_b7": bit(self.bits0, 7),
            "arc_counts": self.arc_counts, "arc_rate": round(self.arc_rate, 2),
        }


def build_run_data(u_set, i_set, p_set, ctrl):
    return struct.pack("<fff", u_set, i_set, p_set) + bytes([ctrl & 0xFF])


def parse_run_reply(data):
    if len(data) < 30:
        raise ProtocolError(f"6040 回應長度不對（{len(data)} bytes）")
    u, i, p = struct.unpack("<fff", data[0:12])
    b0, b1, b2, b3 = data[12:16]
    counts = struct.unpack(">5H", data[16:26])
    rate = struct.unpack("<f", data[26:30])[0]
    names = ("Imax", "UxI", "dU", "Usag", "ArcBurst")
    return RunStatus(u, i, p, b0, b1, b2, b3, dict(zip(names, counts)), rate)


def run(port, u_set, i_set, p_set, ctrl, address=0xFFFF):
    return parse_run_reply(transact(port, CMD_RUN, build_run_data(u_set, i_set, p_set, ctrl), address))
