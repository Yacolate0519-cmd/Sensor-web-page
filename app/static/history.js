/* 實驗紀錄頁：列出過去的實驗，點開後把電源、距離、溫度、光譜儀的數據畫成圖。唯讀，不連 SSE。 */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const API = "/api/history";

  // ------------------------------------------------------------------
  // 圖表定義：kind 對應後端 /series/<kind>；series.key 是該 kind 回傳的欄位
  // ------------------------------------------------------------------
  const SECTIONS = [
    { kind: "psu", title: "電源 TruPlasma", charts: [
      { id: "u", title: "電壓", unit: "V", zero: true, series: [
        { key: "Uavg_A_V", label: "A 極平均", color: "--chart-wave" },
        { key: "Uavg_B_V", label: "B 極平均", color: "--chart-fft" },
        { key: "Uset_V", label: "電壓上限設定", color: "--text-3", dashed: true, noBand: true }] },
      { id: "i", title: "電流", unit: "A", zero: true, series: [
        { key: "Iavg_A_A", label: "A 極平均", color: "--chart-wave" },
        { key: "Iavg_B_A", label: "B 極平均", color: "--chart-fft" },
        { key: "Iset_A", label: "電流上限設定（RMS）", color: "--text-3", dashed: true, noBand: true }] },
      { id: "p", title: "功率", unit: "kW", zero: true, series: [
        { key: "Pavg_A_kW", label: "A 極", color: "--chart-wave" },
        { key: "Pavg_B_kW", label: "B 極", color: "--chart-fft" }] },
      { id: "arc", title: "電弧率", unit: "次/s", zero: true, series: [
        { key: "ArcRate_per_s", label: "電弧率", color: "--warn" }] },
    ] },
    { kind: "distance", title: "距離（測距儀）", charts: [
      { id: "dist", title: "距離", unitFrom: "unit", series: [{ key: "abs", label: "絕對距離", color: "--chart-wave" }] },
    ] },
    { kind: "temperature", title: "溫度", charts: [
      { id: "temp", title: "溫度", unit: "°C", series: [{ key: "temp", label: "溫度", color: "--chart-fft" }] },
    ] },
    { kind: "spectrometer", title: "光譜儀", charts: [
      { id: "spec-total", title: "全波段總強度", unit: "counts", series: [{ key: "total", label: "總強度", color: "--chart-wave" }] },
    ], heat: true },
  ];
  const KIND_LABEL = { psu: "電", distance: "距", temperature: "溫", spectrometer: "光", audio: "音" };
  const KIND_NAME = { psu: "電源", distance: "距離", temperature: "溫度", spectrometer: "光譜儀", audio: "音訊" };
  const STATUS = {
    completed: ["ok", "完成"], recording: ["busy", "錄製中"], interrupted: ["warn", "中斷（未正常結束）"],
    raw_saved: ["warn", "原始資料已存（頻譜未產生）"], unknown: ["off", "舊格式"],
  };
  // 這個警告每次連線都有（韌體測試版），不在圖上標記
  const BENIGN_ALARMS = new Set(["61711"]);

  // ------------------------------------------------------------------
  // 小工具
  // ------------------------------------------------------------------
  const S = { list: [], filter: "", id: null, exp: null, data: {}, full: [0, 1], view: [0, 1],
              outputOn: [], alarms: [], token: 0, hoverT: null, charts: [], heat: null, specPick: null };
  const dpr = () => window.devicePixelRatio || 1;
  const fmtDur = (s) => {
    if (s == null) return "—";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
  };
  const fmtDate = (unix) => {
    if (!unix) return "—";
    const d = new Date(unix * 1000), p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  const fmtBytes = (n) => n >= 1e9 ? (n / 1e9).toFixed(2) + " GB" : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : n >= 1e3 ? (n / 1e3).toFixed(0) + " KB" : n + " B";
  const fmtNum = (v, d = 1) => (v == null || !isFinite(v) ? "—" : Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(d));
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  async function getJSON(url) {
    const r = await fetch(url);
    let body = null;
    try { body = await r.json(); } catch (e) { /* 非 JSON 回應 */ }
    if (!r.ok || !body || body.ok === false) throw new Error((body && body.errors && body.errors[0]) || `HTTP ${r.status}`);
    return body;
  }

  // 軸刻度（與即時頁相同的算法）
  function niceStep(range, target) {
    const raw = range / Math.max(1, target), mag = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / mag;
    return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
  }
  function ticks(min, max, target) {
    if (!(max > min)) return [min];
    const step = niceStep(max - min, target), out = [];
    for (let v = Math.ceil(min / step - 1e-9) * step; v <= max + step * 1e-6; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    return out;
  }
  const tickText = (v, step) => {
    const a = Math.abs(v);
    // 刻度間距小於 1000 時不用 k，否則 50265、50270 都會寫成 50.3k 而分不出來
    if (a >= 10000 && step >= 1000) return (v / 1000).toFixed(a % 1000 === 0 ? 0 : 1) + "k";
    return v.toFixed(step >= 1 ? 0 : Math.min(3, Math.ceil(-Math.log10(step))));
  };
  const lowerBound = (arr, x) => { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < x) lo = m + 1; else hi = m; } return lo; };
  const nearest = (arr, x) => {
    if (!arr.length) return -1;
    const i = lowerBound(arr, x);
    return i === 0 ? 0 : i >= arr.length ? arr.length - 1 : (x - arr[i - 1] < arr[i] - x ? i - 1 : i);
  };

  // ------------------------------------------------------------------
  // 時間軸曲線圖
  // ------------------------------------------------------------------
  const PAD = { l: 58, r: 14, t: 8, b: 24 };

  class Chart {
    constructor(container, def, kind) {
      this.def = def; this.kind = kind; this.resp = null; this.drag = null;
      this.fig = el("figure", "hchart");
      const cap = el("figcaption");
      this.titleEl = el("h4", null, def.title + (def.unit ? ` (${def.unit})` : ""));
      this.legend = el("div", "hlegend");
      this.readout = el("span", "hreadout");
      cap.append(this.titleEl, this.legend, this.readout);
      this.wrap = el("div", "hwrap");
      this.canvas = el("canvas");
      this.note = el("p", "hnote"); this.note.hidden = true;
      this.wrap.append(this.canvas);
      this.fig.append(cap, this.wrap, this.note);
      container.append(this.fig);
      this.buildLegend();
      this.bind();
      new ResizeObserver(() => this.draw()).observe(this.wrap);
    }
    buildLegend() {
      this.legend.textContent = "";
      for (const s of this.def.series) {
        const item = el("span"), sw = el("i", s.dashed ? "dashed" : "");
        sw.style.borderTopColor = `var(${s.color})`;
        item.append(sw, document.createTextNode(s.label));
        this.legend.append(item);
      }
    }
    setData(resp) {
      this.resp = resp;
      if (resp && this.def.unitFrom && resp[this.def.unitFrom]) {
        this.titleEl.textContent = `${this.def.title} (${resp[this.def.unitFrom]})`;
      }
      this.draw();
    }
    setMissing(text) { this.resp = null; this.note.textContent = text; this.note.hidden = false; this.wrap.hidden = true; this.legend.hidden = true; }
    plot() {
      const w = this.wrap.clientWidth, h = 180;
      return { w, h, x0: PAD.l, x1: w - PAD.r, y0: PAD.t, y1: h - PAD.b };
    }
    xOf(t, p) { return p.x0 + (t - S.view[0]) / (S.view[1] - S.view[0]) * (p.x1 - p.x0); }
    tOf(x, p) { return S.view[0] + (x - p.x0) / (p.x1 - p.x0) * (S.view[1] - S.view[0]); }
    yRange() {
      let lo = Infinity, hi = -Infinity;
      const { t, series } = this.resp;
      const i0 = Math.max(0, lowerBound(t, S.view[0]) - 1), i1 = Math.min(t.length, lowerBound(t, S.view[1]) + 1);
      for (const s of this.def.series) {
        const d = series[s.key]; if (!d) continue;
        for (let i = i0; i < i1; i++) {
          const a = d.lo[i] ?? d.v[i], b = d.hi[i] ?? d.v[i];
          if (a != null && a < lo) lo = a;
          if (b != null && b > hi) hi = b;
        }
      }
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      if (this.def.zero) { lo = Math.min(lo, 0); }
      if (hi - lo < 1e-9) { hi = lo + 1; }
      const pad = (hi - lo) * 0.08;
      return [this.def.zero && lo === 0 ? 0 : lo - pad, hi + pad];
    }
    draw() {
      if (!this.resp || this.wrap.hidden) return;
      const p = this.plot(), c = this.canvas, ratio = dpr();
      c.width = Math.round(p.w * ratio); c.height = Math.round(p.h * ratio); c.style.height = p.h + "px";
      const g = c.getContext("2d"); g.setTransform(ratio, 0, 0, ratio, 0, 0);
      const th = { grid: cssVar("--chart-grid"), axis: cssVar("--chart-axis"), tick: cssVar("--chart-tick"),
                   accent: cssVar("--accent"), warn: cssVar("--warn"), mono: cssVar("--mono") };
      const [ymin, ymax] = this.yRange();
      const yOf = (v) => p.y1 - (v - ymin) / (ymax - ymin) * (p.y1 - p.y0);
      g.font = `11px ${th.mono}`; g.textBaseline = "middle";
      // 電源輸出中的底色
      g.globalAlpha = 0.12; g.fillStyle = th.accent;
      for (const [a, b] of S.outputOn) {
        const xa = Math.max(p.x0, this.xOf(a, p)), xb = Math.min(p.x1, this.xOf(b, p));
        if (xb > xa) g.fillRect(xa, p.y0, xb - xa, p.y1 - p.y0);
      }
      g.globalAlpha = 1;
      // 格線與刻度
      g.textAlign = "right"; g.strokeStyle = th.grid; g.fillStyle = th.tick; g.lineWidth = 1;
      const yt = ticks(ymin, ymax, 4), ystep = yt.length > 1 ? yt[1] - yt[0] : 1;
      for (const v of yt) {
        const y = Math.round(yOf(v)) + 0.5;
        g.beginPath(); g.moveTo(p.x0, y); g.lineTo(p.x1, y); g.stroke();
        g.fillText(tickText(v, ystep), p.x0 - 6, y);
      }
      g.textAlign = "center"; g.textBaseline = "top";
      const xt = ticks(S.view[0], S.view[1], Math.max(3, Math.floor(p.w / 90))), xstep = xt.length > 1 ? xt[1] - xt[0] : 1;
      for (const v of xt) {
        const x = Math.round(this.xOf(v, p)) + 0.5;
        g.beginPath(); g.moveTo(x, p.y0); g.lineTo(x, p.y1); g.stroke();
        g.fillText(tickText(v, xstep) + " s", x, p.y1 + 6);
      }
      g.strokeStyle = th.axis; g.strokeRect(p.x0 + 0.5, p.y0 + 0.5, p.x1 - p.x0, p.y1 - p.y0);
      // 資料
      g.save(); g.beginPath(); g.rect(p.x0, p.y0, p.x1 - p.x0, p.y1 - p.y0); g.clip();
      const { t, series, n_raw: nRaw } = this.resp;
      const bucketed = nRaw > t.length;
      for (const s of this.def.series) {
        const d = series[s.key]; if (!d) continue;
        const color = cssVar(s.color);
        if (bucketed && !s.noBand) this.band(g, p, t, d, yOf, color);
        g.strokeStyle = color; g.lineWidth = 1.5; g.setLineDash(s.dashed ? [5, 4] : []);
        g.beginPath();
        let pen = false;
        for (let i = 0; i < t.length; i++) {
          const v = d.v[i];
          if (v == null) { pen = false; continue; }
          const x = this.xOf(t[i], p), y = yOf(v);
          if (pen) g.lineTo(x, y); else { g.moveTo(x, y); pen = true; }
        }
        g.stroke(); g.setLineDash([]);
      }
      // 電源警報標記（連線瞬間常見的無害警告不標）
      if (this.kind === "psu") {
        g.strokeStyle = th.warn; g.fillStyle = th.warn; g.textAlign = "left"; g.textBaseline = "top"; g.setLineDash([2, 3]);
        const seen = new Set(); // 電源會在幾個警告代碼間來回切換，同一個代碼只標第一次出現的位置
        for (const [ta, code] of S.alarms) {
          if (BENIGN_ALARMS.has(code) || seen.has(code)) continue;
          seen.add(code);
          if (ta < S.view[0] || ta > S.view[1]) continue;
          const x = Math.round(this.xOf(ta, p)) + 0.5;
          g.beginPath(); g.moveTo(x, p.y0); g.lineTo(x, p.y1); g.stroke();
          g.fillText(code, x + 3, p.y0 + 2);
        }
        g.setLineDash([]);
      }
      g.restore();
      // 十字線與拖曳選取
      if (S.hoverT != null && S.hoverT >= S.view[0] && S.hoverT <= S.view[1]) {
        const x = Math.round(this.xOf(S.hoverT, p)) + 0.5;
        g.strokeStyle = th.tick; g.globalAlpha = 0.6; g.beginPath(); g.moveTo(x, p.y0); g.lineTo(x, p.y1); g.stroke(); g.globalAlpha = 1;
      }
      if (this.drag) {
        g.fillStyle = th.accent; g.globalAlpha = 0.2;
        const a = Math.min(this.drag.x0, this.drag.x1), b = Math.max(this.drag.x0, this.drag.x1);
        g.fillRect(a, p.y0, b - a, p.y1 - p.y0); g.globalAlpha = 1;
      }
      this.updateReadout();
    }
    band(g, p, t, d, yOf, color) {
      g.globalAlpha = 0.18; g.fillStyle = color; g.beginPath();
      let started = false;
      for (let i = 0; i < t.length; i++) { if (d.hi[i] == null) continue; const x = this.xOf(t[i], p), y = yOf(d.hi[i]); if (started) g.lineTo(x, y); else { g.moveTo(x, y); started = true; } }
      for (let i = t.length - 1; i >= 0; i--) { if (d.lo[i] == null) continue; g.lineTo(this.xOf(t[i], p), yOf(d.lo[i])); }
      g.closePath(); g.fill(); g.globalAlpha = 1;
    }
    updateReadout() {
      if (!this.resp || S.hoverT == null) { this.readout.textContent = ""; return; }
      const i = nearest(this.resp.t, S.hoverT);
      if (i < 0) return;
      const parts = [`${this.resp.t[i].toFixed(1)} s`];
      for (const s of this.def.series) {
        const v = this.resp.series[s.key] && this.resp.series[s.key].v[i];
        parts.push(`${s.label.replace(/（.*）/, "")} ${fmtNum(v, 2)}`);
      }
      this.readout.textContent = parts.join("  ");
    }
    bind() {
      const c = this.canvas;
      const xy = (e) => { const r = c.getBoundingClientRect(); return e.clientX - r.left; };
      c.addEventListener("pointermove", (e) => {
        if (!this.resp) return;
        const p = this.plot(), x = xy(e);
        if (this.drag) this.drag.x1 = x;
        setHover(x >= p.x0 && x <= p.x1 ? this.tOf(x, p) : null);
        if (this.drag) this.draw();
      });
      c.addEventListener("pointerleave", () => { if (!this.drag) setHover(null); });
      c.addEventListener("pointerdown", (e) => { if (!this.resp) return; c.setPointerCapture(e.pointerId); this.drag = { x0: xy(e), x1: xy(e) }; });
      c.addEventListener("pointerup", (e) => {
        if (!this.drag) return;
        const p = this.plot(), d = this.drag; this.drag = null;
        c.releasePointerCapture(e.pointerId);
        if (Math.abs(d.x1 - d.x0) > 8) {
          const a = this.tOf(Math.max(p.x0, Math.min(d.x0, d.x1)), p), b = this.tOf(Math.min(p.x1, Math.max(d.x0, d.x1)), p);
          setView(a, b);
        } else this.draw();
      });
      c.addEventListener("dblclick", () => resetView());
    }
  }

  // ------------------------------------------------------------------
  // 光譜儀熱圖（時間 × 波長）與單一時間點的光譜
  // ------------------------------------------------------------------
  const JET = (() => {
    const lut = new Uint8ClampedArray(256 * 3), c = (v) => Math.max(0, Math.min(1, v));
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      lut[i * 3] = 255 * c(1.5 - Math.abs(4 * t - 3)); lut[i * 3 + 1] = 255 * c(1.5 - Math.abs(4 * t - 2)); lut[i * 3 + 2] = 255 * c(1.5 - Math.abs(4 * t - 1));
    }
    return lut;
  })();

  class HeatChart {
    constructor(container) {
      this.heat = null; this.off = null;
      this.fig = el("figure", "hchart");
      const cap = el("figcaption");
      cap.append(el("h4", null, "光譜熱圖（波長 × 時間，counts）"), el("span", "hlegend", "點一下圖上的位置，下方會顯示那一刻的完整光譜"));
      this.readout = el("span", "hreadout"); cap.append(this.readout);
      this.wrap = el("div", "hwrap"); this.canvas = el("canvas"); this.wrap.append(this.canvas);
      this.note = el("p", "hnote"); this.note.hidden = true;
      this.fig.append(cap, this.wrap, this.note);
      this.specWrap = el("div", "hwrap"); this.specCanvas = el("canvas"); this.specWrap.append(this.specCanvas); this.specWrap.hidden = true;
      this.specTitle = el("h4", null, ""); this.specTitle.hidden = true;
      this.fig.append(this.specTitle, this.specWrap);
      container.append(this.fig);
      new ResizeObserver(() => { this.draw(); this.drawSpec(); }).observe(this.wrap);
      this.canvas.addEventListener("pointermove", (e) => {
        if (!this.heat) return;
        const p = this.plot(), r = this.canvas.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
        if (x < p.x0 || x > p.x1) { setHover(null); return; }
        const t = S.view[0] + (x - p.x0) / (p.x1 - p.x0) * (S.view[1] - S.view[0]);
        setHover(t);
        const wl = this.heat.wl, w = wl[0] + (p.y1 - y) / (p.y1 - p.y0) * (wl[wl.length - 1] - wl[0]);
        this.readout.textContent = y >= p.y0 && y <= p.y1 ? `${t.toFixed(1)} s  ${w.toFixed(0)} nm` : `${t.toFixed(1)} s`;
      });
      this.canvas.addEventListener("pointerleave", () => setHover(null));
      this.canvas.addEventListener("click", (e) => {
        if (!this.heat) return;
        const p = this.plot(), r = this.canvas.getBoundingClientRect(), x = e.clientX - r.left;
        if (x >= p.x0 && x <= p.x1) pickSpectrum(S.view[0] + (x - p.x0) / (p.x1 - p.x0) * (S.view[1] - S.view[0]));
      });
    }
    plot() { const w = this.wrap.clientWidth, h = 240; return { w, h, x0: PAD.l, x1: w - 62, y0: PAD.t, y1: h - PAD.b }; }
    setData(resp) {
      this.heat = resp && resp.heat; this.off = null;
      if (!this.heat) { this.note.textContent = "資料不足，無法畫熱圖"; this.note.hidden = false; this.wrap.hidden = true; return; }
      this.note.hidden = true; this.wrap.hidden = false;
      const { z, zmin, zmax } = this.heat, nt = z.length, nw = z[0].length;
      const off = document.createElement("canvas"); off.width = nt; off.height = nw;
      const img = off.getContext("2d").createImageData(nt, nw), span = Math.max(1e-9, zmax - zmin);
      for (let ti = 0; ti < nt; ti++) for (let wi = 0; wi < nw; wi++) {
        const li = Math.max(0, Math.min(255, Math.round((z[ti][wi] - zmin) / span * 255))) * 3;
        const o = ((nw - 1 - wi) * nt + ti) * 4; // 波長由下往上遞增
        img.data[o] = JET[li]; img.data[o + 1] = JET[li + 1]; img.data[o + 2] = JET[li + 2]; img.data[o + 3] = 255;
      }
      off.getContext("2d").putImageData(img, 0, 0);
      this.off = off; this.draw();
    }
    draw() {
      if (!this.heat || !this.off || this.wrap.hidden) return;
      const p = this.plot(), c = this.canvas, ratio = dpr();
      c.width = Math.round(p.w * ratio); c.height = Math.round(p.h * ratio); c.style.height = p.h + "px";
      const g = c.getContext("2d"); g.setTransform(ratio, 0, 0, ratio, 0, 0);
      const th = { grid: cssVar("--chart-grid"), axis: cssVar("--chart-axis"), tick: cssVar("--chart-tick"), mono: cssVar("--mono"), accent: cssVar("--accent") };
      g.font = `11px ${th.mono}`;
      const { t, wl, zmin, zmax } = this.heat, vs = S.view;
      const xOf = (v) => p.x0 + (v - vs[0]) / (vs[1] - vs[0]) * (p.x1 - p.x0);
      g.save(); g.beginPath(); g.rect(p.x0, p.y0, p.x1 - p.x0, p.y1 - p.y0); g.clip();
      g.imageSmoothingEnabled = false;
      const dt = t.length > 1 ? (t[t.length - 1] - t[0]) / (t.length - 1) : 1;
      g.drawImage(this.off, xOf(t[0] - dt / 2), p.y0, xOf(t[t.length - 1] + dt / 2) - xOf(t[0] - dt / 2), p.y1 - p.y0);
      if (S.hoverT != null) { g.strokeStyle = "#fff"; g.globalAlpha = 0.6; const x = Math.round(xOf(S.hoverT)) + 0.5; g.beginPath(); g.moveTo(x, p.y0); g.lineTo(x, p.y1); g.stroke(); g.globalAlpha = 1; }
      if (S.specPick != null) { g.strokeStyle = th.accent; g.lineWidth = 2; const x = Math.round(xOf(S.specPick)) + 0.5; g.beginPath(); g.moveTo(x, p.y0); g.lineTo(x, p.y1); g.stroke(); }
      g.restore();
      g.strokeStyle = th.axis; g.strokeRect(p.x0 + 0.5, p.y0 + 0.5, p.x1 - p.x0, p.y1 - p.y0);
      g.fillStyle = th.tick; g.textAlign = "right"; g.textBaseline = "middle";
      const w0 = wl[0], w1 = wl[wl.length - 1];
      const wt = ticks(w0, w1, 5), wstep = wt.length > 1 ? wt[1] - wt[0] : 1;
      for (const v of wt) g.fillText(tickText(v, wstep), p.x0 - 6, p.y1 - (v - w0) / (w1 - w0) * (p.y1 - p.y0));
      g.textAlign = "center"; g.textBaseline = "top";
      const xt = ticks(vs[0], vs[1], Math.max(3, Math.floor(p.w / 90))), xs = xt.length > 1 ? xt[1] - xt[0] : 1;
      for (const v of xt) g.fillText(tickText(v, xs) + " s", xOf(v), p.y1 + 6);
      // colorbar
      const cx = p.x1 + 14, cw = 12;
      for (let i = 0; i < p.y1 - p.y0; i++) {
        const li = Math.round(255 * (1 - i / (p.y1 - p.y0))) * 3;
        g.fillStyle = `rgb(${JET[li]},${JET[li + 1]},${JET[li + 2]})`; g.fillRect(cx, p.y0 + i, cw, 1.5);
      }
      g.fillStyle = th.tick; g.textAlign = "left"; g.textBaseline = "middle";
      g.fillText(fmtNum(zmax, 0), cx + cw + 4, p.y0 + 4); g.fillText(fmtNum(zmin, 0), cx + cw + 4, p.y1 - 4);
    }
    showSpectrum(resp) {
      this.spec = resp; this.specTitle.hidden = false; this.specWrap.hidden = false;
      this.specTitle.textContent = `${resp.t.toFixed(1)} s 的光譜（counts）`;
      this.drawSpec();
    }
    drawSpec() {
      if (!this.spec) return;
      const w = this.specWrap.clientWidth, h = 170, p = { x0: PAD.l, x1: w - PAD.r, y0: PAD.t, y1: h - PAD.b };
      const c = this.specCanvas, ratio = dpr();
      c.width = Math.round(w * ratio); c.height = Math.round(h * ratio); c.style.height = h + "px";
      const g = c.getContext("2d"); g.setTransform(ratio, 0, 0, ratio, 0, 0);
      const th = { grid: cssVar("--chart-grid"), axis: cssVar("--chart-axis"), tick: cssVar("--chart-tick"), mono: cssVar("--mono"), line: cssVar("--chart-wave") };
      const { wl, y } = this.spec;
      let lo = Infinity, hi = -Infinity; for (const v of y) if (v != null) { if (v < lo) lo = v; if (v > hi) hi = v; }
      if (!isFinite(lo)) return; if (hi - lo < 1e-9) hi = lo + 1;
      const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
      const xOf = (v) => p.x0 + (v - wl[0]) / (wl[wl.length - 1] - wl[0]) * (p.x1 - p.x0), yOf = (v) => p.y1 - (v - lo) / (hi - lo) * (p.y1 - p.y0);
      g.font = `11px ${th.mono}`; g.strokeStyle = th.grid; g.fillStyle = th.tick; g.lineWidth = 1;
      g.textAlign = "right"; g.textBaseline = "middle";
      const yt = ticks(lo, hi, 4), ys = yt.length > 1 ? yt[1] - yt[0] : 1;
      for (const v of yt) { const yy = Math.round(yOf(v)) + 0.5; g.beginPath(); g.moveTo(p.x0, yy); g.lineTo(p.x1, yy); g.stroke(); g.fillText(tickText(v, ys), p.x0 - 6, yy); }
      g.textAlign = "center"; g.textBaseline = "top";
      const xt = ticks(wl[0], wl[wl.length - 1], Math.max(3, Math.floor(w / 90))), xs = xt.length > 1 ? xt[1] - xt[0] : 1;
      for (const v of xt) { const xx = Math.round(xOf(v)) + 0.5; g.beginPath(); g.moveTo(xx, p.y0); g.lineTo(xx, p.y1); g.stroke(); g.fillText(tickText(v, xs) + " nm", xx, p.y1 + 6); }
      g.strokeStyle = th.axis; g.strokeRect(p.x0 + 0.5, p.y0 + 0.5, p.x1 - p.x0, p.y1 - p.y0);
      g.strokeStyle = th.line; g.lineWidth = 1.2; g.beginPath();
      let pen = false;
      for (let i = 0; i < wl.length; i++) { if (y[i] == null) { pen = false; continue; } const x = xOf(wl[i]), yy = yOf(y[i]); if (pen) g.lineTo(x, yy); else { g.moveTo(x, yy); pen = true; } }
      g.stroke();
    }
  }

  // ------------------------------------------------------------------
  // 檢視範圍與游標（所有圖共用）
  // ------------------------------------------------------------------
  function redrawAll() { S.charts.forEach((c) => c.draw()); if (S.heat) S.heat.draw(); }
  function setHover(t) { S.hoverT = t; redrawAll(); }
  let zoomTimer = null;
  function setView(a, b) {
    if (!(b - a > 1e-6)) return;
    S.view = [a, b]; $("btn-reset").disabled = false;
    redrawAll();
    clearTimeout(zoomTimer); zoomTimer = setTimeout(() => loadAll(false), 150); // 放大後向後端要更細的資料
  }
  function resetView() { S.view = S.full.slice(); $("btn-reset").disabled = true; redrawAll(); loadAll(false); }

  async function pickSpectrum(t) {
    S.specPick = t; S.heat.draw();
    try {
      const r = await getJSON(`${API}/experiments/${S.id}/series/spectrometer?at=${t.toFixed(3)}`);
      if (S.specPick === t) S.heat.showSpectrum(r);
    } catch (e) { S.heat.note.textContent = `讀取光譜失敗：${e.message}`; S.heat.note.hidden = false; }
  }

  // ------------------------------------------------------------------
  // 載入單一實驗
  // ------------------------------------------------------------------
  async function loadAll(initial) {
    const token = ++S.token, id = S.id, exp = S.exp, win = initial ? "" : `&t0=${S.view[0]}&t1=${S.view[1]}`;
    const kinds = SECTIONS.map((s) => s.kind).filter((k) => exp.sensors[k]);
    const results = await Promise.allSettled(kinds.map((k) => getJSON(`${API}/experiments/${id}/series/${k}?max_points=1500${win}`)));
    if (token !== S.token) return; // 期間換了實驗或又縮放了
    results.forEach((r, i) => { if (r.status === "fulfilled") S.data[kinds[i]] = r.value; else S.data[kinds[i]] = { error: r.reason.message }; });
    if (initial) finishInitial();
    for (const c of S.charts) {
      const d = S.data[c.kind];
      if (d && !d.error) c.setData(d);
      else if (d && d.error) c.setMissing(`讀取失敗：${d.error}`);
    }
    if (S.heat && S.data.spectrometer && !S.data.spectrometer.error) S.heat.setData(S.data.spectrometer);
  }

  function finishInitial() {
    const ts = [];
    for (const k of Object.keys(S.data)) { const t = S.data[k].t; if (t && t.length) ts.push(t[0], t[t.length - 1]); }
    const lo = ts.length ? Math.min(0, ...ts) : 0, hi = ts.length ? Math.max(...ts) : 1;
    S.full = [lo, hi > lo ? hi : lo + 1]; S.view = S.full.slice();
    const psu = S.data.psu;
    S.outputOn = psu && psu.output_on ? psu.output_on : []; S.alarms = psu && psu.alarms ? psu.alarms : [];
    renderStats();
  }

  function renderStats() {
    const box = $("d-stats"); box.textContent = "";
    const add = (label, value) => { const d = el("div", "hist-stat"); d.append(el("small", null, label), el("b", null, value)); box.append(d); };
    const psu = S.data.psu, st = psu && psu.stats;
    if (st) {
      add("電源輸出時間", fmtDur(S.outputOn.reduce((a, [x, y]) => a + (y - x), 0)));
      if (st.Uavg_A_V && st.Uavg_B_V) add("最高電壓 A / B", `${fmtNum(st.Uavg_A_V.max, 0)} / ${fmtNum(st.Uavg_B_V.max, 0)} V`);
      if (st.Iavg_A_A && st.Iavg_B_A) add("最大電流 A / B", `${fmtNum(st.Iavg_A_A.max, 1)} / ${fmtNum(st.Iavg_B_A.max, 1)} A`);
      if (st.ArcRate_per_s) add("最大電弧率", `${fmtNum(st.ArcRate_per_s.max, 0)} 次/s`);
      if (psu.regulation) add("調節模式（輸出中）", Object.entries(psu.regulation).map(([k, n]) => `${k}:${n}`).join(" "));
    }
    const dist = S.data.distance;
    if (dist && dist.stats && dist.stats.abs) add("距離範圍", `${fmtNum(dist.stats.abs.min, 1)} ~ ${fmtNum(dist.stats.abs.max, 1)} ${dist.unit}`);
    const tmp = S.data.temperature;
    if (tmp && tmp.stats && tmp.stats.temp) add("溫度範圍", `${fmtNum(tmp.stats.temp.min, 1)} ~ ${fmtNum(tmp.stats.temp.max, 1)} °C`);
    if (tmp && tmp.n_failed) add("溫度讀取失敗", `${tmp.n_failed} / ${tmp.n_rows} 筆`);
    if (dist && dist.n_failed) add("距離無效讀值", `${dist.n_failed} / ${dist.n_rows} 筆`);
  }

  function buildDetail(exp) {
    $("d-title").textContent = exp.title;
    const [, stText] = STATUS[exp.status] || STATUS.unknown;
    $("d-sub").textContent = [exp.id, fmtDate(exp.start_unix), `時長 ${fmtDur(exp.duration_s)}`, stText,
                              exp.stop_reason, exp.simulated ? "模擬資料" : null].filter(Boolean).join(" · ");
    const sp = exp.labels && exp.labels.specimen;
    const extra = [];
    if (exp.labels && exp.labels.power_mode) extra.push(`電源模式 ${exp.labels.power_mode}`);
    if (sp && sp.shape) extra.push(`試片 ${sp.shape === "circle" ? "圓形" : "方形"}`);
    if (extra.length) $("d-sub").textContent += "  |  " + extra.join(" · ");

    const wrap = $("d-charts"); wrap.textContent = ""; S.charts = []; S.heat = null;
    for (const sec of SECTIONS) {
      const section = el("section", "hist-sec"); section.append(el("h3", null, sec.title));
      if (!exp.sensors[sec.kind]) {
        section.append(el("p", "hnote", `這筆實驗沒有${KIND_NAME[sec.kind]}資料`));
        wrap.append(section); continue;
      }
      for (const def of sec.charts) { const c = new Chart(section, def, sec.kind); S.charts.push(c); }
      if (sec.heat) S.heat = new HeatChart(section);
      wrap.append(section);
    }
    if (exp.sensors.audio) {
      const s = el("section", "hist-sec"); s.append(el("h3", null, "音訊"), el("p", "hnote", "這筆實驗有音訊檔（WAV），這一版還不能在頁面上畫成圖，可以從下方的原始檔案下載。"));
      wrap.append(s);
    }
    const files = $("d-files"); files.textContent = "";
    for (const f of exp.files) {
      const li = el("li"), a = el("a", null, f.name);
      a.href = `${API}/experiments/${exp.id}/files/${encodeURIComponent(f.name)}`; a.download = f.name;
      li.append(a, document.createTextNode(`　${fmtBytes(f.size)}`)); files.append(li);
    }
    $("d-files-n").textContent = `${exp.files.length} 個，共 ${fmtBytes(exp.total_bytes)}`;
    const errs = exp.errors || [];
    $("d-errors-box").hidden = !errs.length; $("d-errors-n").textContent = errs.length;
    const ul = $("d-errors"); ul.textContent = "";
    for (const e of errs) ul.append(el("li", null, `${(e.time || "").slice(11, 19)}　${e.message || JSON.stringify(e)}`));
  }

  async function selectExp(id, quiet) {
    S.id = id; S.data = {}; S.specPick = null; S.hoverT = null; S.outputOn = []; S.alarms = [];
    renderList();
    $("empty").hidden = true; $("detail").hidden = false; $("btn-reset").disabled = true;
    $("d-title").textContent = "載入中…"; $("d-sub").textContent = ""; $("d-stats").textContent = ""; $("d-charts").textContent = "";
    if (!quiet) history.replaceState(null, "", "#" + id);
    try {
      const r = await getJSON(`${API}/experiments/${id}`);
      if (S.id !== id) return;
      S.exp = r.experiment; buildDetail(S.exp);
      await loadAll(true);
    } catch (e) {
      if (S.id === id) { $("d-title").textContent = "載入失敗"; $("d-sub").textContent = e.message; }
    }
  }

  // ------------------------------------------------------------------
  // 列表
  // ------------------------------------------------------------------
  function renderList() {
    const q = S.filter.trim().toLowerCase(), ul = $("exp-list"); ul.textContent = "";
    const items = S.list.filter((x) => !q || `${x.id} ${x.title} ${x.status}`.toLowerCase().includes(q));
    $("list-count").textContent = S.list.length
      ? `共 ${S.list.length} 筆${items.length !== S.list.length ? `，符合 ${items.length} 筆` : ""} · ${fmtBytes(S.totalBytes || 0)}` : "還沒有任何實驗紀錄";
    for (const x of items) {
      const li = el("li"), b = el("button", "exp-item"); b.type = "button";
      b.setAttribute("role", "option"); b.setAttribute("aria-selected", String(x.id === S.id));
      const [level, stText] = STATUS[x.status] || STATUS.unknown;
      const title = el("div", "exp-title"), dot = el("span", "dot"); dot.dataset.level = level; dot.title = stText;
      title.append(dot, el("span", null, x.title));
      const meta = el("div", "exp-meta", `${fmtDate(x.start_unix)} · ${fmtDur(x.duration_s)}`);
      const tags = el("div", "exp-tags");
      for (const k of ["psu", "distance", "temperature", "spectrometer", "audio"]) if (x.sensors[k]) { const t = el("span", "tag", KIND_LABEL[k]); t.title = KIND_NAME[k]; tags.append(t); }
      if (x.simulated) tags.append(el("span", "tag tag-sim", "模擬"));
      if (x.status === "interrupted" || x.status === "recording") tags.append(el("span", "tag tag-warn", stText));
      b.append(title, meta, tags); b.addEventListener("click", () => selectExp(x.id)); li.append(b); ul.append(li);
    }
  }

  async function loadList() {
    try {
      const r = await getJSON(`${API}/experiments`);
      S.list = r.items; S.totalBytes = r.total_bytes; renderList();
    } catch (e) { $("list-count").textContent = `載入列表失敗：${e.message}`; }
  }

  $("q").addEventListener("input", (e) => { S.filter = e.target.value; renderList(); });
  $("reload-list").addEventListener("click", loadList);
  $("btn-reset").addEventListener("click", resetView);
  $("btn-refresh").addEventListener("click", () => { if (S.id) selectExp(S.id, true); });
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", redrawAll);

  loadList().then(() => {
    const id = location.hash.slice(1);
    if (id && S.list.some((x) => x.id === id)) selectExp(id, true);
  });
})();
