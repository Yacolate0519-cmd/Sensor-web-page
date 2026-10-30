"""實驗紀錄瀏覽（唯讀）：列出 Sensor_Data/EXP_*，提供各感測器的降採樣曲線給 /history 頁。

只讀實驗資料夾，不寫入、不改檔。時間軸一律換算成「距實驗開始的秒數」：
溫度／電源／光譜儀的 Timestamp 是 ISO 字串，距離是 epoch float（兩者零點不同，見 issue #10），
所以都用 Timestamp 對齊 experiment json 的 start_time_unix，不用各檔的 Elapsed(s)。
"""
import csv
import json
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime

import numpy as np
from flask import Blueprint, jsonify, render_template, request, send_from_directory

EXP_ID_RE = re.compile(r"^EXP_\d{8}_\d{6}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
KIND_PREFIX = {"temperature": "temperature_", "distance": "distance_", "psu": "truplasma_",
               "spectrometer": "spectrometer_"}
MAX_POINTS_LIMIT = 5000
HEAT_MAX_T, HEAT_MAX_WL = 240, 400

# 電源 CSV 要畫的欄位（欄名 → 輸出 key）
PSU_COLS = ["Uset_V", "Iset_A", "Uact_V", "Iact_peak_A", "Uavg_A_V", "Uavg_B_V", "Iavg_A_A", "Iavg_B_A",
            "Pavg_A_kW", "Pavg_B_kW", "Freq_Hz", "Duty_pct", "ArcRate_per_s", "T_OutputCores_C"]

history_bp = Blueprint("history", __name__)
_cfg = {"data_dir": None, "live_id": lambda: None}

# 解析過的 CSV 快取（光譜儀檔可達十幾 MB，縮放時會連續請求）。key 含 mtime 與大小，檔案變動即失效。
_cache = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_MAX = 6


def init(app, data_dir, live_id_fn):
    _cfg["data_dir"] = data_dir
    _cfg["live_id"] = live_id_fn
    app.register_blueprint(history_bp)


# ---------------------------------------------------------------- 檔案與解析
def _exp_dir(exp_id):
    if not EXP_ID_RE.match(exp_id or ""):
        return None
    d = os.path.join(_cfg["data_dir"], exp_id)
    return d if os.path.isdir(d) else None


def _find(d, exp_id, prefix):
    p = os.path.join(d, f"{prefix}{exp_id}.csv")
    return p if os.path.isfile(p) else None


def _read_json(d, exp_id):
    try:
        with open(os.path.join(d, f"experiment_{exp_id}.json"), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _read_csv(path):
    """回傳 (header, rows)。欄數不符的列（寫到一半的最後一行）直接略過。"""
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = [r for r in reader if len(r) == len(header)]
    with _cache_lock:
        _cache[key] = (header, rows)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return header, rows


def _to_unix(s):
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return float("nan")


def _times(rows, start_unix):
    """Timestamp 欄 → 距實驗開始的秒數；start_unix 為 None 時以第一列為 0。"""
    ts = np.array([_to_unix(r[0]) for r in rows], dtype=np.float64)
    if start_unix is None and len(ts):
        start_unix = float(ts[0])
    return ts - (start_unix or 0.0), start_unix


def _num(rows, idx):
    out = np.empty(len(rows), dtype=np.float64)
    for i, r in enumerate(rows):
        try:
            out[i] = float(r[idx])
        except ValueError:
            out[i] = np.nan
    return out


# ---------------------------------------------------------------- 降採樣
def _clean(arr, nd=4):
    return [None if (v != v or v in (float("inf"), float("-inf"))) else round(float(v), nd) for v in arr]


def _bucket(t, cols, max_points):
    """每個 bucket 取平均、最小、最大（保留尖峰）。點數不超過 max_points 時原樣回傳。"""
    n = len(t)
    if n <= max_points:
        return t, {k: (v, v, v) for k, v in cols.items()}
    edges = np.linspace(0, n, max_points + 1).astype(int)
    tt = np.empty(max_points)
    out = {k: (np.full(max_points, np.nan), np.full(max_points, np.nan), np.full(max_points, np.nan))
           for k in cols}
    for b in range(max_points):
        a, z = edges[b], edges[b + 1]
        if z <= a:
            tt[b] = np.nan
            continue
        tt[b] = t[a:z].mean()
        for k, v in cols.items():
            seg = v[a:z]
            seg = seg[~np.isnan(seg)]
            if seg.size:
                out[k][0][b], out[k][1][b], out[k][2][b] = seg.mean(), seg.min(), seg.max()
    keep = ~np.isnan(tt)
    return tt[keep], {k: tuple(x[keep] for x in v) for k, v in out.items()}


def _window(t, t0, t1):
    m = np.ones(len(t), dtype=bool)
    if t0 is not None:
        m &= t >= t0
    if t1 is not None:
        m &= t <= t1
    return m


def _pack(t, cols, max_points, t0, t1):
    m = _window(t, t0, t1)
    tw = t[m]
    sub = {k: v[m] for k, v in cols.items()}
    tt, stats = _bucket(tw, sub, max_points)
    series = {k: {"v": _clean(a), "lo": _clean(b), "hi": _clean(c)} for k, (a, b, c) in stats.items()}
    full = {}
    for k, v in cols.items():  # 摘要用全程資料（不受縮放視窗影響）
        ok = v[~np.isnan(v)]
        full[k] = ({"min": round(float(ok.min()), 4), "max": round(float(ok.max()), 4),
                    "mean": round(float(ok.mean()), 4)} if ok.size else None)
    return {"t": _clean(tt, 3), "series": series, "stats": full, "n_raw": int(m.sum())}


# ---------------------------------------------------------------- 各感測器
def _series_temperature(path, start, p):
    header, rows = _read_csv(path)
    t, _ = _times(rows, start)
    ci = header.index("Temperature(C)")
    v = _num(rows, ci)
    failed = int(np.isnan(v).sum())
    ok = ~np.isnan(v)  # 讀取失敗的列溫度欄是空的，不畫
    out = _pack(t[ok], {"temp": v[ok]}, p["max_points"], p["t0"], p["t1"])
    out.update(unit="°C", n_failed=failed, n_rows=len(rows))
    return out


def _series_distance(path, start, p):
    header, rows = _read_csv(path)
    t, _ = _times(rows, start)
    col = next((c for c in header if c.startswith("Absolute(")), None)
    if col is None:
        raise ValueError("找不到 Absolute 欄")
    unit = col[col.index("(") + 1:col.rindex(")")]
    st = header.index("Status") if "Status" in header else None
    absv = _num(rows, header.index(col))
    valid = ~np.isnan(absv)
    if st is not None:
        valid &= np.array([r[st] == "VALID" for r in rows])
    out = _pack(t[valid], {"abs": absv[valid]}, p["max_points"], p["t0"], p["t1"])
    out.update(unit=unit, n_failed=int((~valid).sum()), n_rows=len(rows))
    return out


def _series_psu(path, start, p):
    header, rows = _read_csv(path)
    t, _ = _times(rows, start)
    cols = {c: _num(rows, header.index(c)) for c in PSU_COLS if c in header}
    out = _pack(t, cols, p["max_points"], p["t0"], p["t1"])
    phase = [r[header.index("Phase")] for r in rows]
    on, seg0 = [], None
    for i, ph in enumerate(phase):  # 輸出中（Phase == on）的區間，前端用來畫底色
        if ph == "on" and seg0 is None:
            seg0 = float(t[i])
        elif ph != "on" and seg0 is not None:
            on.append([round(seg0, 3), round(float(t[i]), 3)])
            seg0 = None
    if seg0 is not None and len(t):
        on.append([round(seg0, 3), round(float(t[-1]), 3)])
    alarms, prev = [], None
    if "AlarmCode" in header:
        ai = header.index("AlarmCode")
        for i, r in enumerate(rows):
            if r[ai] != prev:
                if r[ai]:
                    alarms.append([round(float(t[i]), 3), r[ai]])
                prev = r[ai]
    out.update(output_on=on, alarms=alarms, n_rows=len(rows))
    if "Regulation" in header:
        ri = header.index("Regulation")
        reg = [r[ri] for r, ph in zip(rows, phase) if ph == "on"]
        out["regulation"] = {k: reg.count(k) for k in set(reg)}
    return out


def _pool(a, n_out, axis):
    """沿 axis 以平均值縮成 n_out 份。"""
    n = a.shape[axis]
    if n <= n_out:
        return a
    edges = np.linspace(0, n, n_out + 1).astype(int)
    parts = [np.take(a, range(edges[i], edges[i + 1]), axis=axis).mean(axis=axis) for i in range(n_out)
             if edges[i + 1] > edges[i]]
    return np.stack(parts, axis=axis)


def _series_spectrometer(path, start, p):
    header, rows = _read_csv(path)
    t, _ = _times(rows, start)
    wl = np.array([float(h) for h in header[3:]])
    z = np.array([r[3:] for r in rows], dtype=np.float32) if rows else np.empty((0, len(wl)), np.float32)
    if p.get("at") is not None and len(t):  # 取單一時間點的完整光譜
        i = int(np.argmin(np.abs(t - p["at"])))
        return {"t": round(float(t[i]), 3), "wl": _clean(wl, 2), "y": _clean(z[i], 2)}
    m = _window(t, p["t0"], p["t1"])
    t, z = t[m], z[m]
    out = _pack(t, {"total": z.sum(axis=1).astype(np.float64), "max": z.max(axis=1).astype(np.float64)}
                if len(t) else {"total": np.array([]), "max": np.array([])}, p["max_points"], None, None)
    heat = None
    if len(t) >= 2:
        zt = _pool(_pool(z, min(HEAT_MAX_T, len(t)), 0), min(HEAT_MAX_WL, len(wl)), 1)
        tt = _pool(t[:, None], zt.shape[0], 0)[:, 0]
        ww = _pool(wl[:, None], zt.shape[1], 0)[:, 0]
        heat = {"t": _clean(tt, 3), "wl": _clean(ww, 2), "z": np.round(zt, 1).tolist(),
                "zmin": round(float(np.percentile(zt, 1)), 1), "zmax": round(float(np.percentile(zt, 99.5)), 1)}
    out.update(heat=heat, n_rows=len(rows), wl_range=[float(wl[0]), float(wl[-1])] if len(wl) else None)
    return out


_SERIES = {"temperature": _series_temperature, "distance": _series_distance, "psu": _series_psu,
           "spectrometer": _series_spectrometer}


# ---------------------------------------------------------------- 實驗摘要
def _labels(meta):
    lab = meta.get("experiment_labels") or {}
    return {"metal": lab.get("metal"), "electrolyte": lab.get("electrolyte"), "additive": lab.get("additive"),
            "power_mode": lab.get("power_mode"), "specimen": lab.get("specimen")}


def _summary(exp_id):
    d = os.path.join(_cfg["data_dir"], exp_id)
    meta = _read_json(d, exp_id)
    files, total = [], 0
    for e in sorted(os.scandir(d), key=lambda e: e.name):
        if e.is_file():
            sz = e.stat().st_size
            files.append({"name": e.name, "size": sz})
            total += sz
    names = [f["name"] for f in files]
    sensors = {k: any(n.startswith(pre) and n.endswith(".csv") for n in names) for k, pre in KIND_PREFIX.items()}
    sensors["audio"] = any(n.endswith(".wav") for n in names)
    start_unix = meta.get("start_time_unix")
    if start_unix is None:  # 舊格式沒有 json：用資料夾名稱推開始時間
        try:
            start_unix = datetime.strptime(exp_id[4:], "%Y%m%d_%H%M%S").timestamp()
        except ValueError:
            start_unix = None
    status = meta.get("status") or "unknown"
    if status == "running":
        status = "recording" if _cfg["live_id"]() == exp_id else "interrupted"
    lab = _labels(meta)
    title = " · ".join(str(x) for x in (lab["metal"], lab["electrolyte"], lab["additive"]) if x) or "（未填標籤）"
    return {"id": exp_id, "title": title, "labels": lab, "start_time": meta.get("start_time"),
            "start_unix": start_unix, "duration_s": meta.get("duration_s"), "status": status,
            "stop_reason": meta.get("stop_reason"), "sensors": sensors, "files": files, "total_bytes": total,
            "simulated": bool(meta.get("simulated")), "n_errors": len(meta.get("errors") or []),
            "legacy": not meta, "_meta": meta}


def _public(s):
    return {k: v for k, v in s.items() if k != "_meta"}


# ---------------------------------------------------------------- 路由
@history_bp.get("/history")
def page_history():
    return render_template("history.html")


@history_bp.get("/api/history/experiments")
def api_list():
    root = _cfg["data_dir"]
    ids = sorted((n for n in os.listdir(root) if EXP_ID_RE.match(n) and os.path.isdir(os.path.join(root, n))),
                 reverse=True) if os.path.isdir(root) else []
    items = []
    for i in ids:
        try:
            s = _summary(i)
        except OSError:
            continue
        s = _public(s)
        s.pop("files")  # 列表不需要檔案清單，詳情再給
        items.append(s)
    return jsonify({"ok": True, "items": items, "total_bytes": sum(x["total_bytes"] for x in items)})


@history_bp.get("/api/history/experiments/<exp_id>")
def api_detail(exp_id):
    if _exp_dir(exp_id) is None:
        return jsonify({"ok": False, "errors": ["找不到這筆實驗"]}), 404
    s = _summary(exp_id)
    meta = s["_meta"]
    out = _public(s)
    out.update(errors=meta.get("errors") or [], parameters=meta.get("parameters") or {},
               devices=meta.get("devices") or {}, end_time=meta.get("end_time"))
    return jsonify({"ok": True, "experiment": out})


@history_bp.get("/api/history/experiments/<exp_id>/series/<kind>")
def api_series(exp_id, kind):
    d = _exp_dir(exp_id)
    if d is None or kind not in _SERIES:
        return jsonify({"ok": False, "errors": ["找不到這筆實驗或資料種類"]}), 404
    path = _find(d, exp_id, KIND_PREFIX[kind])
    if path is None:
        return jsonify({"ok": False, "errors": ["這筆實驗沒有這種資料"]}), 404

    def fnum(name):
        try:
            return float(request.args[name])
        except (KeyError, ValueError):
            return None
    try:
        mp = int(request.args.get("max_points", 1500))
    except ValueError:
        mp = 1500
    p = {"max_points": max(50, min(mp, MAX_POINTS_LIMIT)), "t0": fnum("t0"), "t1": fnum("t1"), "at": fnum("at")}
    try:
        data = _SERIES[kind](path, _read_json(d, exp_id).get("start_time_unix"), p)
    except (OSError, ValueError, KeyError) as e:
        return jsonify({"ok": False, "errors": [f"讀取失敗：{type(e).__name__}: {e}"]}), 500
    return jsonify({"ok": True, "kind": kind, **data})


@history_bp.get("/api/history/experiments/<exp_id>/files/<name>")
def api_file(exp_id, name):
    d = _exp_dir(exp_id)
    if d is None or not FILE_RE.match(name) or not os.path.isfile(os.path.join(d, name)):
        return jsonify({"ok": False, "errors": ["找不到檔案"]}), 404
    return send_from_directory(d, name, as_attachment=True, conditional=True)
