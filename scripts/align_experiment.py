"""把一次實驗的電源與各 sensor 資料依 Timestamp 對齊到同一條時間軸，輸出一張表（唯讀，不改原始檔）。

用法（專案根目錄）：
    .venv\\Scripts\\python.exe scripts\\align_experiment.py Sensor_Data\\EXP_20261030_173239 --out aligned.csv [--step 0.5]

時間軸 t_s = Timestamp - experiment json 的 start_time_unix。不用各 CSV 的 Elapsed(s)，
因為距離的零點與其他不同（issue #10）。每個時間格內：數值欄取平均，文字欄（Phase、Regulation）取最後一筆；
同一格沒有資料就留空，不內插、不補值。
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime

import numpy as np

PSU_NUM = ["Uset_V", "Iset_A", "Uact_V", "Iact_peak_A", "Uavg_A_V", "Uavg_B_V", "Iavg_A_A", "Iavg_B_A",
           "Pavg_A_kW", "Pavg_B_kW", "Freq_Hz", "Duty_pct", "ArcRate_per_s", "T_OutputCores_C"]
PSU_TXT = ["Phase", "Regulation", "AlarmCode"]


def to_unix(s):
    try:
        return float(s)
    except ValueError:
        return datetime.fromisoformat(s).timestamp()


def read(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.reader(f)
        h = next(r)
        return h, [row for row in r if len(row) == len(h)]


def num(rows, i):
    out = np.full(len(rows), np.nan)
    for k, r in enumerate(rows):
        try:
            out[k] = float(r[i])
        except ValueError:
            pass
    return out


def load(d, exp_id, prefix):
    p = os.path.join(d, f"{prefix}{exp_id}.csv")
    return read(p) if os.path.isfile(p) else (None, None)


def streams(d, exp_id, t0):
    """回傳 {名稱: (t 陣列, {欄名: 數值陣列}, {欄名: 文字清單})}。"""
    out = {}
    h, rows = load(d, exp_id, "truplasma_")
    if rows:
        t = np.array([to_unix(r[0]) for r in rows]) - t0
        out["psu"] = (t, {c: num(rows, h.index(c)) for c in PSU_NUM if c in h},
                      {c: [r[h.index(c)] for r in rows] for c in PSU_TXT if c in h})
    h, rows = load(d, exp_id, "distance_")
    if rows:
        col = next(c for c in h if c.startswith("Absolute("))
        v, ok = num(rows, h.index(col)), np.array([r[h.index("Status")] == "VALID" for r in rows])
        t = np.array([to_unix(r[0]) for r in rows]) - t0
        out["dist"] = (t[ok], {"dist_" + col[9:-1].replace("µ", "u") + "_abs": v[ok]}, {})
    h, rows = load(d, exp_id, "temperature_")
    if rows:
        v = num(rows, h.index("Temperature(C)")); ok = ~np.isnan(v)
        t = np.array([to_unix(r[0]) for r in rows]) - t0
        out["temp"] = (t[ok], {"temp_C": v[ok]}, {})
    h, rows = load(d, exp_id, "spectrometer_")
    if rows:
        wl = np.array([float(x) for x in h[3:]])
        z = np.array([r[3:] for r in rows], dtype=np.float32)
        t = np.array([to_unix(r[0]) for r in rows]) - t0
        band = (wl >= 450) & (wl <= 650)
        out["spec"] = (t, {"spec_total": z.sum(1).astype(float), "spec_450_650nm_mean": z[:, band].mean(1).astype(float)}, {})
    return out


def align(st, step, t_end):
    n = int(np.ceil(t_end / step)) + 1
    cols = {"t_s": np.round(np.arange(n) * step, 3)}
    for name, (t, nums, txts) in st.items():
        idx = np.floor(t / step).astype(int)
        keep = (idx >= 0) & (idx < n)
        cnt = np.bincount(idx[keep], minlength=n)
        cols[f"n_{name}"] = cnt
        for c, v in nums.items():
            s = np.bincount(idx[keep], weights=np.nan_to_num(v[keep]), minlength=n)
            with np.errstate(invalid="ignore", divide="ignore"):
                cols[c] = np.where(cnt > 0, s / cnt, np.nan)
        for c, lst in txts.items():
            last = np.array([""] * n, dtype=object)
            for i, k in enumerate(idx):
                if 0 <= k < n:
                    last[k] = lst[i]
            cols[c] = last
    return cols


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 終端機預設 cp1252，中文會讓 print 出錯
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exp_dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--step", type=float, default=0.5, help="時間格（秒），預設 0.5")
    a = ap.parse_args()
    d = os.path.abspath(a.exp_dir)
    exp_id = os.path.basename(d)
    with open(os.path.join(d, f"experiment_{exp_id}.json"), encoding="utf-8-sig") as f:
        meta = json.load(f)
    st = streams(d, exp_id, meta["start_time_unix"])
    t_end = meta.get("duration_s") or max(float(t[-1]) for t, _, _ in st.values())
    cols = align(st, a.step, t_end)
    names = list(cols)
    with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(names)
        for i in range(len(cols["t_s"])):
            w.writerow(["" if (isinstance(cols[c][i], float) and np.isnan(cols[c][i])) else cols[c][i] for c in names])
    print(f"{exp_id}: {len(cols['t_s'])} 列 × {len(names)} 欄，時間格 {a.step}s → {a.out}")


if __name__ == "__main__":
    sys.exit(main())
