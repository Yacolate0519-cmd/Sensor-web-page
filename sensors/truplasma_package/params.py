"""Channel table for the TruPlasma Bipolar 4010 (manual p.75-77, values checked 2026-10-22/24).

`writable=False` entries are shown but never written: communication, Profibus and parallel
settings could cut this program off from the unit (baud, control source, address, timeout).
"""
from dataclasses import dataclass, asdict

U_NOM, I_NOM, P_NOM = 1000.0, 25.0, 10.0  # V, A, kW (4010 rating, manual p.16)


@dataclass(frozen=True)
class Param:
    channel: int
    kind: str          # byte / int / float / dint
    name: str
    group: str
    unit: str = ""
    lo: float | None = None
    hi: float | None = None
    writable: bool = True
    fmt: str = "num"   # num / bool / bits / mode
    note: str = ""

    def to_dict(self):
        return asdict(self)


ARC_CRITERIA = "bit0 dU、bit1 UxI、bit2 Imax、bit3 Usag、bit4 ArcBurst、bit5 ATG"

PARAMS = [
    # 脈衝與頻率（面板 Mode / Configuration）
    Param(33, "byte", "脈衝模式", "pulse", fmt="mode",
          note="bit mask：0 DC-A、1 DC-B、2 MonoPulse-A、3 MonoPulse-B、4 Full、5 BiPulse、6 Trapez、7 Step。寫入方式未在實機驗證"),
    Param(51130, "float", "頻率設定值", "pulse", "kHz", 0.1, 100),
    Param(51131, "float", "佔空比設定值", "pulse", "%", 1, 99),
    Param(650, "byte", "Off Time", "pulse", "%", 1, 20, note="10/22 讀到 80，超出手冊範圍，意義待確認"),
    Param(140, "byte", "低頻模式", "pulse", fmt="bool"),
    Param(1100, "int", "低頻設定值", "pulse", "Hz", 1, 1000),
    Param(652, "byte", "上升時間 A", "pulse", "%", 0, 30),
    Param(653, "byte", "下降時間 A", "pulse", "%", 0, 30),
    Param(654, "byte", "上升時間 B", "pulse", "%", 0, 30),
    Param(655, "byte", "下降時間 B", "pulse", "%", 0, 30),
    Param(803, "byte", "功率平衡（Power Equalizer）", "pulse", fmt="bool"),
    # 電弧管理（面板 Arc Mgmt）
    Param(200, "byte", "電弧判斷條件", "arc", fmt="bits", note=ARC_CRITERIA),
    Param(209, "byte", "電弧設定", "arc", fmt="bits", note="bit0 反向脈衝、bit1 硬電弧後軟啟動"),
    Param(202, "byte", "電弧計數溢位", "arc", fmt="bool"),
    Param(205, "float", "Imax 門檻", "arc", "A", 2.5, 32.5),
    Param(207, "float", "Ix 門檻", "arc", "A", 2.5, 25),
    Param(208, "float", "Ux 門檻", "arc", "V", 0, 800),
    Param(250, "float", "硬電弧中斷時間", "arc", "µs", 10, 2000),
    Param(251, "float", "微電弧中斷時間", "arc", "µs", 10, 1000),
    Param(256, "float", "dU 門檻", "arc", "%", 10, 100),
    Param(252, "byte", "輸出電壓下降比例（Sag）", "arc", "%", 10, 50),
    Param(261, "byte", "Imax 偏移", "arc", "%", 0, 100),
    Param(265, "byte", "Arc Burst 連續次數", "arc", "次", 1, 100),
    Param(270, "int", "電弧率門檻", "arc", "arc/s", 0, 40000),
    Param(275, "int", "Arc Burst 中斷時間", "arc", "µs", 25, 10000),
    Param(276, "int", "Arc Burst 導通時間下限", "arc", "µs", 1, 1000),
    Param(1512, "byte", "電弧同步", "arc", fmt="bits", note="bit0 dU、bit1 UxI、bit2 Imax、bit3 Usag"),
    # 點火（面板 Ignition）
    Param(451, "byte", "點火啟用", "ignition", fmt="bool"),
    Param(454, "byte", "停用點火電壓判斷", "ignition", fmt="bool"),
    Param(450, "float", "點火電流門檻", "ignition", "A", 0, 2.5),
    Param(453, "float", "點火電壓門檻", "ignition", "V", 200, 1000),
    # 通訊／並聯：只顯示，不寫入
    Param(18, "byte", "啟用的介面", "comm", fmt="bits", writable=False,
          note="bit0 RS232、1 Analog、2 EtherCat、3 Display、5 Profibus"),
    Param(19, "byte", "開機控制來源", "comm", fmt="bits", writable=False),
    Param(20, "byte", "目前控制來源", "comm", fmt="bits", writable=False),
    Param(28, "byte", "RS232 鮑率", "comm", fmt="bits", writable=False, note="bit4 = 115200"),
    Param(3, "int", "RS485 位址", "comm", writable=False),
    Param(5, "int", "通訊逾時", "comm", "s", writable=False),
    Param(120, "byte", "並聯模式", "comm", writable=False),
    Param(1200, "byte", "並聯從機數", "comm", writable=False),
    Param(65461, "float", "並聯 Psetmax", "comm", "kW", writable=False),
    Param(65455, "float", "Iset 上限", "comm", "A", writable=False),
]

PARAMS_BY_CH = {p.channel: p for p in PARAMS}

# Read-only measurements polled every cycle alongside the 6040 heartbeat.
MEASUREMENTS = [
    (966, "Uavg_A_V"), (967, "Iavg_A_A"), (980, "Pavg_A_kW"),
    (976, "Uavg_B_V"), (977, "Iavg_B_A"), (981, "Pavg_B_kW"),
    (51140, "Freq_Hz"), (51141, "Duty_pct"), (932, "T_OutputCores_C"),
]


def check_value(param, value):
    """Return the value converted to the channel type, or raise ValueError with a Chinese message."""
    if not param.writable:
        raise ValueError(f"{param.name}（ch{param.channel}）不開放寫入")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{param.name}：不是數字") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{param.name}：不是有效數字")
    if param.kind != "float":
        if v != int(v):
            raise ValueError(f"{param.name}：必須是整數")
        v = int(v)
    if param.fmt == "bool" and v not in (0, 1):
        raise ValueError(f"{param.name}：只能是 0 或 1")
    if param.fmt == "mode" and v not in [1 << n for n in range(8)]:
        raise ValueError(f"{param.name}：一次只能選一種模式")
    if param.fmt == "bits" and not 0 <= v <= 255:
        raise ValueError(f"{param.name}：必須在 0–255")
    if param.lo is not None and v < param.lo:
        raise ValueError(f"{param.name}：不能小於 {param.lo:g} {param.unit}")
    if param.hi is not None and v > param.hi:
        raise ValueError(f"{param.name}：不能大於 {param.hi:g} {param.unit}")
    return v


def check_setpoints(u, i, p):
    out = []
    for name, value, hi, unit in (("電壓", u, U_NOM, "V"), ("電流", i, I_NOM, "A"), ("功率", p, P_NOM, "kW")):
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name}設定值不是數字") from None
        if v != v or not 0 <= v <= hi:
            raise ValueError(f"{name}設定值必須在 0–{hi:g} {unit}")
        out.append(v)
    return tuple(out)
