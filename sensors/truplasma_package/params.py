"""Channel table for the TruPlasma Bipolar 4010 (manual p.75-77, values checked 2026-10-22/24).

`writable=False` entries are shown but never written: communication, Profibus and parallel
settings could cut this program off from the unit (baud, control source, address, timeout).
"""
from dataclasses import dataclass, asdict

# 上限待學長確認，暫用額定值
RECIPE_LIMITS = {"u": 1000.0, "i": 25.0, "p_default": 10.0, "p": 10.0}

U_NOM, I_NOM, P_NOM = 1000.0, 25.0, 10.0  # V, A, kW (4010 rating, manual p.16)

OFF_TIME_MANUAL_MAX = 20  # 手冊 ch650 Off Time 範圍 0–20 %
OFF_TIME_MAX = 49         # 本程式接受的上限：a、b > 0 時 off = (100−a−b)/2 ≤ 49


def off_time_exceeds_manual(off):
    """Off Time（週期 %）是否超出手冊範圍（>20）：面板可接受，但波形尚未用示波器驗證。"""
    return off is not None and off > OFF_TIME_MANUAL_MAX


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
    Param(650, "byte", "Off Time", "pulse", "%", 1, OFF_TIME_MAX, note="手冊範圍 0–20；2026-11-02 面板實測接受 36；10/22 讀到 80（意義待確認）"),
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
    for name, value, hi, unit in (("電壓 U", u, U_NOM, "V"), ("電流 I", i, I_NOM, "A"), ("功率 P", p, P_NOM, "kW")):
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name}設定值不是數字") from None
        if v != v or not 0 <= v <= hi:
            raise ValueError(f"{name}設定值必須在 0–{hi:g} {unit}")
        out.append(v)
    return tuple(out)


def bipulse_timing(pos_pct, neg_pct):
    """雙極脈衝：正向導通 a %、負向導通 b % → (占空比 ch51131, 關斷時間 ch650)，皆為週期的 %。

    【假設，未在真機驗證】手冊 p.26：TA = Duty·T − Off、TB = (1−Duty)·T − Off，一個週期是
    正向→關斷→負向→關斷。這裡假設 ch650 的單位是「週期的 %」，於是
        off  = (100 − a − b) / 2     （必須是 1–49 的整數，ch650 是 byte）
        duty = a + off               （必須在 1–99）
    實機 ch650 曾讀到 80（超出手冊範圍），單位可能不是這個假設；上機驗證後只需改這個函式。
    2026-11-02：手冊寫 Off Time 0–20 %，但面板接受並存入 36（讀回 ch650=36），所以 20 不是硬限制；
    實驗室要做 20/20（off=30）。目前「假設公式在 off>20 時仍成立」，尚未用示波器驗證；
    off>20 仍放行，由 off_time_exceeds_manual(off) 判斷並在網頁／實驗紀錄標「超出手冊範圍」。
    """
    try:
        a, b = float(pos_pct), float(neg_pct)
    except (TypeError, ValueError):
        raise ValueError("正向／負向導通 % 必須是數字") from None
    if not (a == a and b == b) or abs(a) == float("inf") or abs(b) == float("inf"):
        raise ValueError("正向／負向導通 % 不是有效數字")
    if a <= 0 or b <= 0:
        raise ValueError("正向／負向導通 % 必須大於 0")
    total = a + b
    if total != int(total) or int(total) % 2:
        raise ValueError(f"正向 + 負向導通 % 必須是偶數（目前 {total:g}），關斷時間才會是整數")
    off = (100 - total) / 2
    if not 1 <= off <= OFF_TIME_MAX:
        raise ValueError(f"正向 + 負向導通 % 需在 2–98 之間的偶數（目前 {total:g}，關斷時間 {off:g} % 超出 1–{OFF_TIME_MAX}）")
    duty = a + off
    if not 1 <= duty <= 99:
        raise ValueError(f"換算後佔空比 {duty:g} % 超出 1–99")
    return duty, int(off)


def plan_recipe(current, payload):
    """驗證完整配方並回傳需要寫入的通道。"""
    if not isinstance(payload, dict):
        raise ValueError("配方必須是物件")
    mode = payload.get("mode")
    modes = {"bipulse": 32, "mono_a": 4, "mono_b": 8}
    if not isinstance(mode, str) or mode not in modes:
        raise ValueError("脈衝模式不正確")
    def number(key, label, low, high, default=None):
        try:
            v = float(payload.get(key, default))
        except (TypeError, ValueError):
            raise ValueError(f"{label}必須是數字") from None
        if not low <= v <= high:
            raise ValueError(f"{label}必須在 {low:g}–{high:g}")
        return v
    freq = number("freq_hz", "頻率 f（Hz）", 1000 if mode == "bipulse" else 1, 50000 if mode == "bipulse" else 100000)
    if mode == "bipulse":
        duty, off = bipulse_timing(number("pos_pct", "正向導通（%）", 1, 98), number("neg_pct", "負向導通（%）", 1, 98))
    else:
        duty = number("duty_pct", "佔空比（%）", 1, 99)
    for key, label in (("u", "電壓 U（V）"), ("i", "電流 I（A）"), ("p", "功率 P（kW）")):
        value = number(key, label, 0, RECIPE_LIMITS[key], RECIPE_LIMITS["p_default"] if key == "p" else None)
        if key == "p" and value == 0:
            raise ValueError("功率上限 P 必須大於 0，否則電源不會輸出")
    low = mode != "bipulse" and freq <= 1000
    desired = [(33, modes[mode]), (140, int(low)),
               (1100, round(freq)) if low else (51130, round(freq / 1000, 1)), (51131, duty)]
    if mode == "bipulse":
        desired.append((650, off))
    return [(ch, value) for ch, value in desired
            if current.get(ch, current.get(str(ch))) is None or abs(current.get(ch, current.get(str(ch))) - value) > 1e-5]
