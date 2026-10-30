/* 感測器整合系統：網頁版前端。無外部函式庫，圖表以 <canvas> 繪製。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PARAM_KEYS = ["com_port", "audio_device", "sample_rate",
    "update_interval", "history_duration", "refl_mode", "distance_interval",
    "spec_interval", "spec_integration_ms"];
  // 實驗標籤（在「輸入實驗標籤」視窗內填寫）：[表單 id, 名稱]，必填檢查與 app/web_monitor.py 的 LABEL_FIELDS 一致
  const LABEL_REQUIRED = [["label_metal", "金屬種類"], ["label_electrolyte", "電解液配方與濃度"]];
  // 試片尺寸（長度 mm、面積 mm²）：形狀 → [表單 id, 名稱]；對應 web_monitor.py 的 SPECIMEN_SHAPES
  const SPECIMEN_DIMS = {
    square: [["label_specimen_length", "長"], ["label_specimen_width", "寬"], ["label_specimen_thickness", "厚"]],
    circle: [["label_specimen_area", "面積"], ["label_specimen_thickness", "厚度"]],
  };
  PARAM_KEYS.push("label_metal", "label_electrolyte", "label_additive",
    "label_specimen_length", "label_specimen_width", "label_specimen_thickness", "label_specimen_area");
  const AUDIO_PLACEHOLDERS = ["無可用音訊設備", "音訊設備檢測失敗"];

  let theme = readTheme();
  let state = null;          // 最近一次伺服器狀態
  let currentRun = null;     // 目前圖表所屬的 run_id
  let busy = false;          // 開始流程進行中
  let syncing = true;        // 正在從 /api/state 復原
  let pendingAudio = [];     // 復原期間暫存的 SSE 音訊事件
  let pendingSeries = [];    // 復原期間暫存的 SSE 溫度／距離事件
  let pendingSpec = [];      // 復原期間暫存的 SSE 光譜儀事件（["meta"|"col", msg]）
  let optMeta = null;        // 光譜儀 meta（波長軸等）；每個 run 只送一次
  const seenNotices = new Set();

  // ------------------------------------------------------------------
  // 主題
  // ------------------------------------------------------------------
  function readTheme() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    return {
      bg: v("--chart-bg"), grid: v("--chart-grid"), axis: v("--chart-axis"),
      tick: v("--chart-tick"), text: v("--text-2"), faint: v("--text-3"),
      wave: v("--chart-wave"), fft: v("--chart-fft"), // 沿用既有色票：距離用藍、溫度用紅
      warn: v("--warn"), font: v("--font"), mono: v("--mono"),
    };
  }
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
    theme = readTheme();
    charts.forEach((c) => c.requestDraw());
  });

  // ------------------------------------------------------------------
  // 軸刻度
  // ------------------------------------------------------------------
  function niceStep(range, target) {
    const raw = range / Math.max(1, target);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
    return step * mag;
  }
  function ticks(min, max, target) {
    if (!(max > min)) return [min];
    const step = niceStep(max - min, target);
    const out = [];
    for (let v = Math.ceil(min / step - 1e-9) * step; v <= max + step * 1e-6; v += step) {
      out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    }
    return out;
  }
  function fmtTick(v, step) {
    const abs = Math.abs(v);
    if (abs >= 10000) return (v / 1000).toFixed(abs % 1000 === 0 ? 0 : 1) + "k";
    const dec = step >= 1 ? 0 : Math.min(3, Math.ceil(-Math.log10(step)));
    return v.toFixed(dec);
  }

  // ------------------------------------------------------------------
  // Canvas 基礎：處理 devicePixelRatio 與 resize
  // ------------------------------------------------------------------
  class CanvasChart {
    constructor(canvas, margin) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.margin = margin;
      this.w = 0; this.h = 0; this.dpr = 1;
      this._raf = 0;
      new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
      this.resize();
    }
    resize() {
      const r = this.canvas.parentElement.getBoundingClientRect();
      this.dpr = window.devicePixelRatio || 1;
      this.w = Math.max(1, Math.floor(r.width));
      this.h = Math.max(1, Math.floor(r.height));
      this.canvas.width = Math.round(this.w * this.dpr);
      this.canvas.height = Math.round(this.h * this.dpr);
      this.requestDraw();
    }
    requestDraw() {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = 0; this.draw(); });
    }
    get plot() {
      const m = this.margin;
      return { x: m.l, y: m.t, w: Math.max(10, this.w - m.l - m.r), h: Math.max(10, this.h - m.t - m.b) };
    }
    begin() {
      const ctx = this.ctx;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.w, this.h);
      const p = this.plot;
      ctx.fillStyle = theme.bg;
      ctx.fillRect(p.x, p.y, p.w, p.h);
      return p;
    }
    axes(p, x, y, labels, opts = {}) {
      const ctx = this.ctx;
      const xt = ticks(x[0], x[1], Math.max(3, Math.floor(p.w / 90)));
      const yt = ticks(y[0], y[1], Math.max(3, Math.floor(p.h / 45)));
      const xs = xt.length > 1 ? xt[1] - xt[0] : 1;
      const ys = yt.length > 1 ? yt[1] - yt[0] : 1;
      const X = (v) => p.x + (v - x[0]) / (x[1] - x[0]) * p.w;
      const Y = (v) => p.y + p.h - (v - y[0]) / (y[1] - y[0]) * p.h;

      if (!opts.noGrid) {
        ctx.strokeStyle = theme.grid; ctx.lineWidth = 1;
        ctx.beginPath();
        xt.forEach((v) => { const px = Math.round(X(v)) + 0.5; ctx.moveTo(px, p.y); ctx.lineTo(px, p.y + p.h); });
        yt.forEach((v) => { const py = Math.round(Y(v)) + 0.5; ctx.moveTo(p.x, py); ctx.lineTo(p.x + p.w, py); });
        ctx.stroke();
      }
      ctx.strokeStyle = theme.axis;
      ctx.strokeRect(p.x + 0.5, p.y + 0.5, p.w - 1, p.h - 1);

      ctx.fillStyle = theme.tick;
      ctx.font = `11px ${theme.mono}`;
      ctx.textAlign = "center"; ctx.textBaseline = "top";
      let lastRight = -Infinity;
      xt.forEach((v) => {
        const px = X(v); const s = fmtTick(v, xs); const tw = ctx.measureText(s).width;
        if (px - tw / 2 < lastRight + 6 || px < p.x - 1 || px > p.x + p.w + 1) return;
        ctx.fillText(s, px, p.y + p.h + 5); lastRight = px + tw / 2;
      });
      ctx.textAlign = "right"; ctx.textBaseline = "middle";
      yt.forEach((v) => {
        const py = Y(v); if (py < p.y - 1 || py > p.y + p.h + 1) return;
        ctx.fillText(fmtTick(v, ys), p.x - 6, py);
      });

      ctx.fillStyle = theme.text;
      ctx.font = `11.5px ${theme.font}`;
      ctx.textAlign = "center"; ctx.textBaseline = "bottom";
      ctx.fillText(labels[0], p.x + p.w / 2, this.h - 2);
      ctx.save();
      ctx.translate(12, p.y + p.h / 2); ctx.rotate(-Math.PI / 2);
      ctx.textBaseline = "middle";
      ctx.fillText(labels[1], 0, 0);
      ctx.restore();
      return { X, Y };
    }
    placeholder(p, text) {
      const ctx = this.ctx;
      ctx.fillStyle = theme.faint;
      ctx.font = `12.5px ${theme.font}`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(text, p.x + p.w / 2, p.y + p.h / 2);
    }
  }

  // ------------------------------------------------------------------
  // 時間序列折線圖（溫度／距離）：x = 開始監測起的經過秒數，顯示完整曲線；null 值斷線
  // ------------------------------------------------------------------
  const SERIES_KEEP = 4000;     // 前端累積超過此點數就降採樣
  const SERIES_TARGET = 2000;   // 降採樣後的目標點數

  // 與後端 downsample_series 同邏輯：分桶後每段連續有效值留 min/max，每段連續 null 留一個 null 點
  function downsampleSeries(pts, target) {
    const n = pts.length;
    if (n <= target) return pts;
    const buckets = Math.max(1, target >> 1);
    const out = [];
    const flush = (run) => {
      let lo = run[0], hi = run[0];
      for (const q of run) { if (q[1] < lo[1]) lo = q; if (q[1] > hi[1]) hi = q; }
      if (lo === hi) out.push(lo); else if (lo[0] < hi[0]) out.push(lo, hi); else out.push(hi, lo);
    };
    for (let b = 0; b < buckets; b++) {
      let run = [], noneOpen = false;
      for (let i = Math.floor(n * b / buckets), e = Math.floor(n * (b + 1) / buckets); i < e; i++) {
        const q = pts[i];
        if (q[1] === null) {
          if (run.length) { flush(run); run = []; }
          if (!noneOpen) { out.push(q); noneOpen = true; }
        } else { noneOpen = false; run.push(q); }
      }
      if (run.length) flush(run);
    }
    return out;
  }

  class SeriesChart extends CanvasChart {
    constructor(canvas, opts) {
      super(canvas, { l: 62, r: 14, t: 8, b: 38 });
      this.opts = opts;       // { labels, color, unit, minRange, decimals }
      this.disabled = false;
      this.statusText = "";  // 感測器目前狀態文字（最新值為 None 時顯示在警告標籤上）
      this.clear();
    }
    clear() {
      this.pts = []; this.lo = Infinity; this.hi = -Infinity;
      this.requestDraw();
    }
    setAll(points) { this.clear(); this.add(points); }
    add(points) {
      const last = this.pts.length ? this.pts[this.pts.length - 1][0] : -Infinity;
      for (const q of points) {
        if (q[0] < last) continue; // 復原與 SSE 重疊：略過已有的時間
        this.pts.push(q);
        if (q[1] !== null) { if (q[1] < this.lo) this.lo = q[1]; if (q[1] > this.hi) this.hi = q[1]; }
      }
      if (this.pts.length > SERIES_KEEP) this.pts = downsampleSeries(this.pts, SERIES_TARGET);
      this.requestDraw();
    }
    yRange() {
      let lo = this.lo, hi = this.hi;
      if (!(hi >= lo)) return [0, 1];
      const min = this.opts.minRange;
      if (hi - lo < min) { const mid = (hi + lo) / 2; lo = mid - min / 2; hi = mid + min / 2; }
      const pad = (hi - lo) * 0.08;
      return [lo - pad, hi + pad];
    }
    draw() {
      const p = this.begin();
      const pts = this.pts;
      const tEnd = pts.length ? pts[pts.length - 1][0] : 0;
      const x = [0, Math.max(10, tEnd)];
      const y = this.yRange();
      const { X, Y } = this.axes(p, x, y, this.opts.labels);
      if (!pts.length) { this.placeholder(p, this.disabled ? "未啟用" : "等待資料"); return; }
      const ctx = this.ctx;
      ctx.save();
      ctx.beginPath(); ctx.rect(p.x, p.y, p.w, p.h); ctx.clip();
      const color = this.opts.color();
      ctx.strokeStyle = color; ctx.fillStyle = color;
      ctx.lineWidth = 1.25; ctx.lineJoin = "round";
      ctx.beginPath();
      let pen = false, single = null; // pen：目前這段線已落筆；single：只有一個點的區段（補畫圓點）
      const dots = [];
      for (let i = 0; i < pts.length; i++) {
        const q = pts[i];
        if (q[1] === null) { if (single) dots.push(single); pen = false; single = null; continue; }
        const px = X(q[0]), py = Y(q[1]);
        if (!pen) { ctx.moveTo(px, py); pen = true; single = [px, py]; }
        else { ctx.lineTo(px, py); single = null; }
      }
      if (single) dots.push(single);
      ctx.stroke();
      dots.forEach(([px, py]) => { ctx.beginPath(); ctx.arc(px, py, 1.6, 0, Math.PI * 2); ctx.fill(); });
      ctx.restore();
      this.drawLatest(p, X, Y, color);
    }
    // 最新值標籤：圓點 + 圓角標籤，垂直置中於最後一個有效點；最新一筆為 null 時改警告樣式
    drawLatest(p, X, Y, color) {
      const pts = this.pts;
      let k = pts.length - 1;
      while (k >= 0 && pts[k][1] === null) k--;
      if (k < 0) return; // 完全沒有有效點
      const failed = k !== pts.length - 1;
      const px = X(pts[k][0]), py = Y(pts[k][1]);
      const ctx = this.ctx;
      const text = failed ? (this.statusText || "讀取失敗") : `${pts[k][1].toFixed(this.opts.decimals)} ${this.opts.unit}`;
      const bg = failed ? theme.warn : color;
      ctx.beginPath(); ctx.arc(px, py, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = bg; ctx.fill();
      ctx.lineWidth = 1.5; ctx.strokeStyle = theme.bg; ctx.stroke();
      ctx.font = `600 11.5px ${theme.mono}`;
      const padX = 7, bh = 20, bw = Math.ceil(ctx.measureText(text).width) + padX * 2;
      // 預設放點的左側；左側放不下才貼繪圖區內側；y 夾在繪圖區內
      const bx = Math.max(p.x + 3, Math.min(px - 9 - bw, p.x + p.w - bw - 3));
      const by = Math.max(p.y + 2, Math.min(py - bh / 2, p.y + p.h - bh - 2));
      ctx.beginPath(); ctx.roundRect(bx, by, bw, bh, 5);
      ctx.fillStyle = bg; ctx.fill();
      ctx.fillStyle = failed ? "#1a1400" : "#fff";
      ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillText(text, bx + padX, by + bh / 2 + 0.5);
    }
  }

  // ------------------------------------------------------------------
  // 頻譜圖（滾動熱圖 + colorbar）
  // ------------------------------------------------------------------
  const JET = (() => {
    const lut = new Uint8ClampedArray(256 * 3);
    const c = (v) => Math.max(0, Math.min(1, v));
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      lut[i * 3] = 255 * c(1.5 - Math.abs(4 * t - 3));
      lut[i * 3 + 1] = 255 * c(1.5 - Math.abs(4 * t - 2));
      lut[i * 3 + 2] = 255 * c(1.5 - Math.abs(4 * t - 1));
    }
    return lut;
  })();
  const EMPTY = -32768;
  const MAX_TEX_WIDTH = 8192;

  // colorbar（Spectrogram 與光譜熱圖共用）：JET 色階、刻度與直式標題
  function drawColorbar(chart, p, vmin, vmax, label) {
    const ctx = chart.ctx;
    const bx = p.x + p.w + 14, bw = 12, by = p.y, bh = p.h;
    for (let i = 0; i < bh; i++) {
      const li = Math.round((1 - i / Math.max(1, bh - 1)) * 255) * 3;
      ctx.fillStyle = `rgb(${JET[li]},${JET[li + 1]},${JET[li + 2]})`;
      ctx.fillRect(bx, by + i, bw, 1.5);
    }
    ctx.strokeStyle = theme.axis;
    ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, bh - 1);
    const yt = ticks(vmin, vmax, Math.max(3, Math.floor(bh / 45)));
    const step = yt.length > 1 ? yt[1] - yt[0] : 1;
    ctx.fillStyle = theme.tick; ctx.font = `11px ${theme.mono}`;
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    yt.forEach((v) => {
      const py = by + bh - (v - vmin) / (vmax - vmin) * bh;
      ctx.fillRect(bx + bw, py, 3, 1);
      ctx.fillText(fmtTick(v, step), bx + bw + 5, py);
    });
    ctx.save();
    ctx.fillStyle = theme.text; ctx.font = `11.5px ${theme.font}`;
    ctx.translate(chart.w - 8, by + bh / 2); ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(label, 0, 0);
    ctx.restore();
  }

  class Spectrogram extends CanvasChart {
    constructor(canvas) {
      super(canvas, { l: 62, r: 78, t: 8, b: 38 });
      this.meta = null;
      this.vmin = -20; this.vmax = 60;
      this.lastRange = 0;
      this.off = document.createElement("canvas");
      this.offCtx = this.off.getContext("2d");
    }
    configure(meta) {
      this.meta = meta;
      const nf = meta.n_freq;
      this.nf = nf;
      this.k = Math.max(1, Math.ceil(meta.max_cols / MAX_TEX_WIDTH)); // 每個像素欄合併的 specgram 欄數
      this.width = Math.max(1, Math.ceil(meta.max_cols / this.k));
      this.raw = new Int16Array(this.width * nf).fill(EMPTY);
      this.off.width = this.width; this.off.height = nf;
      this.offCtx.clearRect(0, 0, this.width, nf);
      this.colImg = this.offCtx.createImageData(1, nf);
      this.total = 0;
      this.hasRange = false;
      this.requestDraw();
    }
    reset(meta) { this.configure(meta); }
    get groups() { return this.total ? Math.floor((this.total - 1) / this.k) + 1 : 0; }

    addColumns(start, count, data) {
      if (!this.meta) return;
      const nf = this.nf, k = this.k, W = this.width;
      const touched = new Set();
      for (let j = 0; j < count; j++) {
        const i = start + j;
        if (i < this.total) continue; // 已經有了（復原與 SSE 重疊）
        const g = Math.floor(i / k), pos = g % W, base = pos * nf, src = j * nf;
        const fresh = (i % k === 0) || (Math.floor((this.total - 1) / k) !== g);
        for (let f = 0; f < nf; f++) {
          const v = data[src + f];
          if (fresh || this.raw[base + f] === EMPTY || v > this.raw[base + f]) this.raw[base + f] = v;
        }
        this.total = i + 1;
        touched.add(pos);
      }
      const now = performance.now();
      if (!this.hasRange || now - this.lastRange > 1000) {
        this.lastRange = now;
        if (this.autoRange()) { this.repaintAll(); this.requestDraw(); return; }
      }
      touched.forEach((pos) => this.paintColumn(pos));
      this.requestDraw();
    }
    autoRange() {
      // 以最近的資料取百分位（2% / 99.5%）決定色階
      const G = this.groups; if (!G) return false;
      const nf = this.nf, W = this.width;
      const recent = Math.min(G, W, 600);
      const stride = Math.max(1, Math.floor(recent * nf / 40000));
      const sample = [];
      let idx = 0;
      for (let g = G - recent; g < G; g++) {
        const base = (g % W) * nf;
        for (let f = 0; f < nf; f++, idx++) {
          if (idx % stride) continue;
          const v = this.raw[base + f];
          if (v !== EMPTY) sample.push(v / 10);
        }
      }
      if (sample.length < 16) return false;
      sample.sort((a, b) => a - b);
      const q = (p) => sample[Math.min(sample.length - 1, Math.floor(p * (sample.length - 1)))];
      let lo = q(0.02), hi = q(0.995);
      if (hi - lo < 10) { const mid = (hi + lo) / 2; lo = mid - 5; hi = mid + 5; }
      lo = Math.floor(lo); hi = Math.ceil(hi);
      const changed = !this.hasRange || Math.abs(lo - this.vmin) > 1.5 || Math.abs(hi - this.vmax) > 1.5;
      if (changed) { this.vmin = lo; this.vmax = hi; this.hasRange = true; }
      return changed;
    }
    color(v, out, o) {
      if (v === EMPTY) { out[o + 3] = 0; return; }
      let t = (v / 10 - this.vmin) / (this.vmax - this.vmin);
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      const li = Math.round(t * 255) * 3;
      out[o] = JET[li]; out[o + 1] = JET[li + 1]; out[o + 2] = JET[li + 2]; out[o + 3] = 255;
    }
    paintColumn(pos) {
      const nf = this.nf, d = this.colImg.data, base = pos * this.nf;
      for (let f = 0; f < nf; f++) this.color(this.raw[base + f], d, (nf - 1 - f) * 4); // 低頻在下
      this.offCtx.putImageData(this.colImg, pos, 0);
    }
    repaintAll() {
      const nf = this.nf, W = this.width;
      const img = this.offCtx.createImageData(W, nf), d = img.data;
      for (let pos = 0; pos < W; pos++) {
        const base = pos * nf;
        for (let f = 0; f < nf; f++) this.color(this.raw[base + f], d, ((nf - 1 - f) * W + pos) * 4);
      }
      this.offCtx.putImageData(img, 0, 0);
    }
    draw() {
      const p = this.begin();
      const m = this.meta;
      const H = m ? m.history_duration : 100;
      const fmax = m ? m.freq_max : 11025;
      const secPerGroup = m ? this.k * m.hop / m.sample_rate : 1;
      const G = this.groups;
      const tEnd = G * secPerGroup;
      const x = tEnd <= H ? [0, H] : [tEnd - H, tEnd];
      const ctx = this.ctx;

      if (m && G) {
        const vis = Math.min(G, this.width);
        const g0 = G - vis;
        const tx = (t) => p.x + (t - x[0]) / (x[1] - x[0]) * p.w;
        ctx.save();
        ctx.beginPath(); ctx.rect(p.x, p.y, p.w, p.h); ctx.clip();
        ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
        const p0 = g0 % this.width;
        const first = Math.min(vis, this.width - p0);
        const blit = (srcX, n, gStart) => {
          const dx0 = tx(gStart * secPerGroup), dx1 = tx((gStart + n) * secPerGroup);
          ctx.drawImage(this.off, srcX, 0, n, this.nf, dx0, p.y, dx1 - dx0, p.h);
        };
        blit(p0, first, g0);
        if (first < vis) blit(0, vis - first, g0 + first);
        ctx.restore();
      }
      this.axes(p, x, [0, fmax], ["Time (s)", "Frequency (Hz)"], { noGrid: true });
      if (!m || !G) this.placeholder(p, m ? "等待資料" : "尚未開始監測");
      this.colorbar(p);
    }
    colorbar(p) { drawColorbar(this, p, this.vmin, this.vmax, "Amplitude (dB)"); }
  }

  // ------------------------------------------------------------------
  // 光譜熱圖（x = 時間、y = 波長）：每筆光譜是一個 512-bin 的欄，欄位置用實際經過秒數；
  // 色階取近期 1%–99% 百分位；讀取失敗的時間欄留空並以警告色淡淡標出。
  // ------------------------------------------------------------------
  class SpectrumHeatmap extends CanvasChart {
    constructor(canvas) {
      super(canvas, { l: 62, r: 78, t: 8, b: 38 });
      this.meta = null;
      this.disabled = false;
      this.vmin = 0; this.vmax = 1000; this.hasRange = false; this.lastRange = 0;
      this.off = document.createElement("canvas");
      this.offCtx = this.off.getContext("2d");
      this.total = 0;
    }
    configure(meta) {
      this.meta = meta;
      const nb = meta.n_bins, W = meta.max_cols;
      this.nb = nb; this.W = W;
      this.raw = new Uint16Array(W * nb);
      this.ts = new Float64Array(W);
      this.ok = new Uint8Array(W);
      this.off.width = W; this.off.height = nb;
      this.offCtx.clearRect(0, 0, W, nb);
      this.colImg = this.offCtx.createImageData(1, nb);
      this.total = 0; this.hasRange = false;
      this.requestDraw();
    }
    clear() { this.meta = null; this.total = 0; this.hasRange = false; this.requestDraw(); }
    lastOk() { return this.total ? this.ok[(this.total - 1) % this.W] === 1 : true; }
    lastT() { return this.total ? this.ts[(this.total - 1) % this.W] : 0; }
    // bins 為 null 代表該筆讀取失敗（欄留空）
    addColumn(idx, t, bins) {
      if (!this.meta || idx < this.total) return; // 已有（復原與 SSE 重疊）
      const pos = idx % this.W;
      this.ts[pos] = t; this.ok[pos] = bins ? 1 : 0;
      if (bins) this.raw.set(bins, pos * this.nb); else this.raw.fill(0, pos * this.nb, (pos + 1) * this.nb);
      this.total = idx + 1;
      this.finishAdd([pos]);
    }
    // 復原：一次補多欄（all 為各欄 bins 串接）
    addColumns(start, ts, oks, all) {
      if (!this.meta) return;
      const touched = [];
      for (let j = 0; j < ts.length; j++) {
        const idx = start + j;
        if (idx < this.total) continue;
        const pos = idx % this.W;
        this.ts[pos] = ts[j]; this.ok[pos] = oks[j] ? 1 : 0;
        this.raw.set(all.subarray(j * this.nb, (j + 1) * this.nb), pos * this.nb);
        this.total = idx + 1;
        touched.push(pos);
      }
      this.finishAdd(touched);
    }
    finishAdd(touched) {
      const now = performance.now();
      if (!this.hasRange || now - this.lastRange > 1000) {
        this.lastRange = now;
        if (this.autoRange()) { this.repaintAll(); this.requestDraw(); return; }
      }
      touched.forEach((pos) => this.paintColumn(pos));
      this.requestDraw();
    }
    autoRange() {
      // 近期成功欄的 1% / 99% 百分位，避免單一尖峰把整張圖壓暗
      const nb = this.nb, W = this.W;
      const recent = Math.min(this.total, W, 300);
      const stride = Math.max(1, Math.floor(recent * nb / 40000));
      const sample = [];
      let idx = 0;
      for (let i = this.total - recent; i < this.total; i++) {
        const pos = i % W;
        if (!this.ok[pos]) continue;
        const base = pos * nb;
        for (let f = 0; f < nb; f++, idx++) if (idx % stride === 0) sample.push(this.raw[base + f]);
      }
      if (sample.length < 16) return false;
      sample.sort((a, b) => a - b);
      const q = (p) => sample[Math.min(sample.length - 1, Math.floor(p * (sample.length - 1)))];
      let lo = q(0.01), hi = q(0.99);
      if (hi - lo < 50) { const mid = (hi + lo) / 2; lo = mid - 25; hi = mid + 25; }
      lo = Math.floor(lo); hi = Math.ceil(hi);
      const tol = Math.max(5, (this.vmax - this.vmin) * 0.03);
      const changed = !this.hasRange || Math.abs(lo - this.vmin) > tol || Math.abs(hi - this.vmax) > tol;
      if (changed) { this.vmin = lo; this.vmax = hi; this.hasRange = true; }
      return changed;
    }
    colorAt(v, out, o) {
      let t = (v - this.vmin) / (this.vmax - this.vmin);
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      const li = Math.round(t * 255) * 3;
      out[o] = JET[li]; out[o + 1] = JET[li + 1]; out[o + 2] = JET[li + 2]; out[o + 3] = 255;
    }
    paintColumn(pos) {
      const nb = this.nb, d = this.colImg.data, base = pos * nb;
      if (!this.ok[pos]) { d.fill(0); } // 失敗欄：透明（draw 時另外淡淡標色）
      else for (let f = 0; f < nb; f++) this.colorAt(this.raw[base + f], d, (nb - 1 - f) * 4); // 短波長在下
      this.offCtx.putImageData(this.colImg, pos, 0);
    }
    repaintAll() {
      for (let i = Math.max(0, this.total - this.W); i < this.total; i++) this.paintColumn(i % this.W);
    }
    draw() {
      const p = this.begin();
      const m = this.meta;
      const H = m ? m.display_s : 100;
      const tEnd = this.total ? this.lastT() + (m ? m.interval : 0.5) : 0;
      const x = tEnd <= H ? [0, H] : [tEnd - H, tEnd];
      const y = m ? [m.wl_min, m.wl_max] : [175, 1322];
      const ctx = this.ctx;
      if (m && this.total) {
        const tx = (t) => p.x + (t - x[0]) / (x[1] - x[0]) * p.w;
        ctx.save();
        ctx.beginPath(); ctx.rect(p.x, p.y, p.w, p.h); ctx.clip();
        ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
        for (let i = Math.max(0, this.total - this.W); i < this.total; i++) {
          const pos = i % this.W, t0 = this.ts[pos];
          const t1 = i + 1 < this.total ? this.ts[(i + 1) % this.W] : t0 + m.interval;
          if (t1 < x[0]) continue;
          const dx0 = tx(t0), dw = Math.max(1, tx(t1) - dx0 + 0.6);
          if (this.ok[pos]) ctx.drawImage(this.off, pos, 0, 1, this.nb, dx0, p.y, dw, p.h);
          else { ctx.globalAlpha = 0.3; ctx.fillStyle = theme.warn; ctx.fillRect(dx0, p.y, dw, p.h); ctx.globalAlpha = 1; }
        }
        ctx.restore();
      }
      this.axes(p, x, y, ["Time (s)", "Wavelength (nm)"], { noGrid: true });
      if (!m || !this.total) this.placeholder(p, this.disabled ? "未啟用" : m ? "等待資料" : "尚未開始監測");
      drawColorbar(this, p, this.vmin, this.vmax, "Intensity (counts)");
    }
  }

  // ------------------------------------------------------------------
  // 即時光譜（x = 波長、y = counts）：顯示最新一筆完整解析度，標示前 3 高峰；飽和時角落警告；
  // 讀取失敗時保留最後一筆並顯示狀態文字。
  // ------------------------------------------------------------------
  const SAT_WARN = 65000;
  class LiveSpectrum extends CanvasChart {
    constructor(canvas, heat) {
      super(canvas, { l: 62, r: 78, t: 8, b: 38 });
      this.heat = heat;
      this.wl = null; this.data = null; this.t = 0;
      this.disabled = false; this.statusText = "";
      this.peaks = [];
    }
    clear() { this.wl = null; this.data = null; this.peaks = []; this.requestDraw(); }
    setWavelength(wl) { this.wl = Float64Array.from(wl); this.data = null; this.peaks = []; this.requestDraw(); }
    setData(arr, t) { this.data = arr; this.t = t; this.peaks = this.findPeaks(); this.requestDraw(); }
    // 前 3 高峰：區域極大值、彼此至少相隔 8 nm、需明顯高於背景；以三點拋物線內插峰位
    findPeaks() {
      const d = this.data, wl = this.wl;
      if (!d || !wl || d.length !== wl.length) return [];
      const n = d.length;
      const sorted = Array.from(d).sort((a, b) => a - b);
      const floor = sorted[n >> 1] + Math.max(100, 0.05 * sorted[n - 1]);
      const cand = [];
      for (let i = 1; i < n - 1; i++) if (d[i] >= floor && d[i] >= d[i - 1] && d[i] >= d[i + 1]) cand.push(i);
      cand.sort((a, b) => d[b] - d[a]);
      const out = [];
      for (const i of cand) {
        if (out.some((q) => Math.abs(wl[q.i] - wl[i]) < 8)) continue;
        const a = d[i - 1], b = d[i], c = d[i + 1], den = a - 2 * b + c;
        const off = den < 0 ? Math.max(-0.5, Math.min(0.5, 0.5 * (a - c) / den)) : 0;
        const w = wl[i] + off * (wl[Math.min(n - 1, i + 1)] - wl[Math.max(0, i - 1)]) / 2;
        out.push({ i, w, v: b });
        if (out.length === 3) break;
      }
      return out;
    }
    draw() {
      const p = this.begin();
      const wl = this.wl, d = this.data;
      const x = wl ? [wl[0], wl[wl.length - 1]] : [175, 1322];
      let top = 1000;
      if (d) { let mx = 0; for (let i = 0; i < d.length; i++) if (d[i] > mx) mx = d[i]; top = Math.max(1000, mx * 1.12); }
      const { X, Y } = this.axes(p, x, [0, top], ["Wavelength (nm)", "Intensity (counts)"]);
      if (!d || !wl) { this.placeholder(p, this.disabled ? "未啟用" : "等待資料"); return; }
      const ctx = this.ctx;
      ctx.save();
      ctx.beginPath(); ctx.rect(p.x, p.y, p.w, p.h); ctx.clip();
      ctx.strokeStyle = theme.wave; ctx.lineWidth = 1.2; ctx.lineJoin = "round";
      ctx.beginPath();
      for (let i = 0; i < d.length; i++) {
        const px = X(wl[i]), py = Y(d[i]);
        if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py);
      }
      ctx.stroke();
      ctx.restore();
      // 峰值標籤
      ctx.font = `600 11px ${theme.mono}`; ctx.textBaseline = "bottom"; ctx.textAlign = "center";
      this.peaks.forEach((q) => {
        const px = X(q.w), py = Y(q.v);
        ctx.fillStyle = theme.fft;
        ctx.beginPath(); ctx.arc(px, py, 2.5, 0, Math.PI * 2); ctx.fill();
        const text = `${q.w.toFixed(1)} nm`, tw = ctx.measureText(text).width;
        const tx = Math.max(p.x + tw / 2 + 2, Math.min(px, p.x + p.w - tw / 2 - 2));
        ctx.fillStyle = theme.text;
        ctx.fillText(text, tx, Math.max(p.y + 12, py - 5));
      });
      // 角落警告：飽和（右上）、讀取失敗（左上，保留最後一筆）
      const badge = (text, right, y) => {
        ctx.font = `600 11.5px ${theme.mono}`;
        const bw = Math.ceil(ctx.measureText(text).width) + 14, bh = 20;
        const bx = right ? p.x + p.w - bw - 6 : p.x + 6;
        ctx.beginPath(); ctx.roundRect(bx, y, bw, bh, 5);
        ctx.fillStyle = theme.warn; ctx.fill();
        ctx.fillStyle = "#1a1400"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
        ctx.fillText(text, bx + 7, y + bh / 2 + 0.5);
      };
      let max = 0; for (let i = 0; i < d.length; i++) if (d[i] > max) max = d[i];
      if (max >= SAT_WARN) badge("飽和：已達 65535 counts 上限", true, p.y + 6);
      if (!this.heat.lastOk()) {
        const age = Math.max(0, Math.round(this.heat.lastT() - this.t));
        badge(`${this.statusText || "讀取失敗"}（顯示 ${age} 秒前的光譜）`, false, p.y + 6);
      }
    }
  }

  const specChart = new Spectrogram($("cv-spec"));
  const tempChart = new SeriesChart($("cv-temp"), {
    labels: ["Time (s)", "Temperature (°C)"],
    color: () => theme.fft, unit: "°C", minRange: 1, decimals: 1,
  });
  const distChart = new SeriesChart($("cv-dist"), {
    labels: ["Time (s)", "Distance (µm)"],
    color: () => theme.wave, unit: "µm", minRange: 500, decimals: 1,
  });
  const psuUChart = new SeriesChart($("cv-psu-u"), {
    labels: ["Time (s)", "PSU Voltage (V)"],
    color: () => theme.wave, unit: "V", minRange: 10, decimals: 1,
  });
  const psuIChart = new SeriesChart($("cv-psu-i"), {
    labels: ["Time (s)", "PSU Current, peak (A)"],
    color: () => theme.fft, unit: "A", minRange: 0.5, decimals: 2,
  });
  // key 對應 /api/state 的 series；psu_* 不是感測器（沒有 sensors／enabled 項目），renderState 會略過
  const seriesCharts = { temp: tempChart, distance: distChart, psu_u: psuUChart, psu_i: psuIChart };
  const optHeat = new SpectrumHeatmap($("cv-opt"));
  const optLive = new LiveSpectrum($("cv-opt-live"), optHeat);
  const charts = [specChart, tempChart, distChart, psuUChart, psuIChart, optHeat, optLive];

  function b64Bytes(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }
  const b64U16 = (b64) => new Uint16Array(b64Bytes(b64).buffer);

  function applySeries(msg) {
    if (msg.run_id !== currentRun) return;
    Object.entries(msg.points || {}).forEach(([k, pts]) => seriesCharts[k].add(pts));
  }

  function applyAudio(msg) {
    if (msg.run_id !== currentRun) return;
    if (msg.spec && msg.spec.count) {
      const arr = new Int16Array(b64Bytes(msg.spec.b64).buffer);
      if (msg.spec.start > specChart.total) { resync(); return; } // 有漏欄：重新抓整段
      specChart.addColumns(msg.spec.start, msg.spec.count, arr);
    }
  }

  // ---- 光譜儀（Optical Spectrum） ----
  function applySpecMeta(meta) {
    if (meta.run_id !== currentRun || (optMeta && optMeta.run_id === meta.run_id)) return;
    optMeta = meta;
    optHeat.configure(meta);
    optLive.setWavelength(meta.wavelength);
    $("opt-note").textContent = `${meta.wl_min.toFixed(0)}–${meta.wl_max.toFixed(0)} nm · 積分 ${meta.integration_ms} ms · 每 ${meta.interval} 秒 · 顯示 ${Math.round(meta.display_s)} 秒`;
  }
  function applySpecCol(msg) {
    if (msg.run_id !== currentRun) return;
    if (!optMeta) { resync(); return; }           // 還沒收到波長軸：重新復原
    if (msg.idx > optHeat.total) { resync(); return; } // 有漏筆：重新抓整段
    optHeat.addColumn(msg.idx, msg.t, msg.bins ? b64U16(msg.bins) : null);
    if (msg.full) optLive.setData(b64U16(msg.full), msg.t); else optLive.requestDraw();
  }
  function restoreSpectrum(sp) {
    if (!sp) return;
    applySpecMeta(sp.meta);
    if (sp.count) optHeat.addColumns(sp.start, sp.t, sp.ok, b64U16(sp.bins));
    if (sp.latest) optLive.setData(b64U16(sp.latest.full), sp.latest.t);
  }

  function setupCharts(meta, runId) {
    currentRun = runId;
    optMeta = null; optHeat.clear(); optLive.clear();
    $("opt-note").textContent = "AvaSpec-ULS2048L";
    specChart.configure(meta);
    $("spec-note").textContent =
      `NFFT ${meta.nfft} · overlap ${meta.noverlap} · ${meta.sample_rate} Hz · 顯示 ${meta.history_duration} 秒`;
    Object.values(seriesCharts).forEach((c) => c.clear());
  }

  // ------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    return { status: res.status, ok: res.ok, data };
  }

  // ------------------------------------------------------------------
  // 裝置清單
  // ------------------------------------------------------------------
  async function loadCom(keepValue) {
    const btn = $("refresh-com"); btn.classList.add("spinning");
    try {
      const { data } = await api("/api/devices?kind=com");
      const com = data.com;
      const list = $("com-list"); list.innerHTML = "";
      com.ports.forEach((p) => {
        const o = document.createElement("option");
        o.value = p.device; o.label = p.description || p.device; list.appendChild(o);
      });
      if (!keepValue) $("com_port").value = com.default || "";
      $("com-hint").textContent = com.error ? `列舉失敗：${com.error}`
        : com.ports.length ? `找到 ${com.ports.length} 個候選埠，可直接輸入` : "未偵測到序列埠，可直接輸入";
    } catch (e) {
      $("com-hint").textContent = "無法取得序列埠清單";
    } finally { btn.classList.remove("spinning"); }
  }

  async function loadAudio(keepValue) {
    const btn = $("refresh-audio"); btn.classList.add("spinning");
    const sel = $("audio_device");
    const prev = sel.value;
    try {
      const { data } = await api("/api/devices?kind=audio");
      const a = data.audio;
      sel.innerHTML = "";
      if (a.devices.length) {
        a.devices.forEach((d) => {
          const o = document.createElement("option");
          o.value = d.display_name; o.textContent = d.display_name; sel.appendChild(o);
        });
      } else {
        const o = document.createElement("option");
        o.value = a.placeholder; o.textContent = a.placeholder; sel.appendChild(o);
      }
      sel.value = keepValue && [...sel.options].some((o) => o.value === prev) ? prev : a.default;
    } catch (e) {
      sel.innerHTML = '<option value="音訊設備檢測失敗">音訊設備檢測失敗</option>';
    } finally { btn.classList.remove("spinning"); }
  }

  function ensureOption(sel, value) {
    if (!value) return;
    if (![...sel.options].some((o) => o.value === value)) {
      const o = document.createElement("option"); o.value = value; o.textContent = value; sel.appendChild(o);
    }
    sel.value = value;
  }

  // ------------------------------------------------------------------
  // 表單
  // ------------------------------------------------------------------
  function readForm() {
    const out = {};
    PARAM_KEYS.forEach((k) => {
      const el = $(k);
      out[k] = el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value;
    });
    out.label_specimen_shape = specimenShape();
    return out;
  }
  function writeForm(params) {
    PARAM_KEYS.forEach((k) => {
      if (params[k] === undefined || k.startsWith("label_")) return; // 實驗標籤每次重填，不帶入上一筆
      const el = $(k);
      if (el.type === "checkbox") el.checked = params[k] === "1";
      else if (el.tagName === "SELECT") ensureOption(el, params[k]);
      else el.value = params[k];
    });
  }

  // ------------------------------------------------------------------
  // 狀態渲染
  // ------------------------------------------------------------------
  function setDot(el, level) { el.querySelector(".dot").dataset.level = level; }

  // ------------------------------------------------------------------
  // 感測器狀態卡（狀態卡片內）：燈號、失敗原因分類、排查建議、最後正常時間、連續失敗次數
  // 原因代碼與 app/web_monitor.py 的 R_* / REASON_LABELS 一致；列由 monitor.html 的 data-sensor 決定，
  // 伺服器 sensors 缺少該 key 時（尚未接入的感測器）顯示「未接入」。
  // ------------------------------------------------------------------
  const REASON_LABELS = {
    idle: "待機", init: "初始化中", ok: "正常", stopped: "已停止",
    driver: "驅動／韌體問題", not_found: "找不到裝置", busy: "Port 衝突／被佔用",
    no_data: "數據收不進來", wiring: "接線／訊號異常", out_of_range: "超出量程",
    disabled: "未選擇／未啟用", error: "其他錯誤",
  };
  const REASON_TONES = {
    ok: "ok",
    busy: "warn", no_data: "warn", wiring: "warn", out_of_range: "warn",
    driver: "error", not_found: "error", error: "error",
    idle: "neutral", init: "neutral", disabled: "neutral", stopped: "neutral",
  };
  // 狀態卡底色：綠＝正常運行、紅＝錯誤／斷線（含讀取失敗等警告）、其餘（待機、未啟用、儲存中）一律橙
  const CARD_LEVELS = { ok: "ok", warn: "error", error: "error" };
  let serverOffsetMs = 0; // 伺服器時間 - 本機時間，用 server_time 對時避免本機時鐘差

  function fmtAgo(sec) {
    sec = Math.max(0, Math.floor(sec));
    if (sec < 60) return `${sec} 秒前`;
    if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒前`;
    return `${Math.floor(sec / 3600)} 小時 ${Math.floor((sec % 3600) / 60)} 分前`;
  }

  function updateSensorMeta(row) {
    const meta = row.querySelector(".sr-meta");
    meta.hidden = row.querySelector(".sr-last").hidden && row.querySelector(".sr-fails").hidden;
  }

  function renderSensorAgo() {
    if (!state) return;
    const now = (Date.now() + serverOffsetMs) / 1000;
    document.querySelectorAll("#sensor-panel .sensor-row").forEach((row) => {
      const s = state.sensors[row.dataset.sensor];
      const el = row.querySelector(".sr-last");
      el.hidden = !(s && s.last_ok);
      if (!el.hidden) el.textContent = `最後正常 ${fmtAgo(now - s.last_ok)}`;
      updateSensorMeta(row);
    });
  }

  function renderSensorPanel(s) {
    if (s.server_time) serverOffsetMs = s.server_time * 1000 - Date.now();
    document.querySelectorAll("#sensor-panel .sensor-row").forEach((row) => {
      const sensor = s.sensors[row.dataset.sensor];
      const badge = row.querySelector(".sr-badge");
      const detail = row.querySelector(".sr-detail");
      const fails = row.querySelector(".sr-fails");
      if (!sensor) { // 尚未整合的感測器
        row.querySelector(".dot").dataset.level = "off";
        row.dataset.level = "idle";
        badge.textContent = "未接入"; badge.dataset.tone = "neutral";
        detail.hidden = true; fails.hidden = true;
        return;
      }
      const reason = sensor.reason || "idle";
      row.querySelector(".dot").dataset.level = sensor.level;
      row.dataset.level = CARD_LEVELS[sensor.level] || "idle";
      badge.textContent = REASON_LABELS[reason] || reason;
      badge.dataset.tone = REASON_TONES[reason] || "neutral";
      // 說明文字是「短現象：原因與建議」：短現象常駐顯示，原因與建議收在可展開的區塊（重繪時保留展開狀態）
      let more = detail.querySelector("details");
      if (!more) {
        detail.innerHTML = '<details class="sr-more"><summary></summary><div class="sr-adv"></div></details>';
        more = detail.querySelector("details");
      }
      const text = sensor.detail || "", cut = text.indexOf("：");
      const adv = cut >= 0 ? text.slice(cut + 1) : "";
      more.querySelector("summary").textContent = cut >= 0 ? text.slice(0, cut) : text;
      more.querySelector(".sr-adv").textContent = adv;
      more.classList.toggle("no-adv", !adv);
      detail.hidden = !text;
      fails.hidden = !sensor.fail_count;
      fails.textContent = `連續失敗 ${sensor.fail_count} 次`;
    });
    renderSensorAgo();
  }

  // 秒數 → 「分:秒」，滿一小時改「時:分:秒」（長時間實驗用秒數不好讀）
  function fmtClock(sec) {
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60, p = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
  }
  const fmtSpan = (sec) => (sec = Math.round(sec)) >= 3600 ? `${Math.floor(sec / 3600)} 時 ${Math.floor(sec % 3600 / 60)} 分 ${sec % 60} 秒`
    : sec >= 60 ? `${Math.floor(sec / 60)} 分 ${sec % 60} 秒` : `${sec} 秒`;

  function renderTime(elapsedInt, elapsed, duration, phase) {
    $("time-num").textContent = fmtClock(elapsedInt);
    $("time-unit").textContent = elapsedInt >= 3600 ? "時:分:秒" : "分:秒";
    const bar = $("time-bar"), prog = bar.parentElement;
    const d = duration;
    if (phase === "idle") {
      prog.classList.remove("indeterminate"); bar.style.width = "0";
      $("time-sub").textContent = "按「停止監測」才會結束";
      return;
    }
    if (d > 0) {
      prog.classList.remove("indeterminate");
      bar.style.width = `${Math.min(100, elapsed / d * 100)}%`;
      const rem = Math.max(0, Math.ceil(d - elapsed));
      $("time-sub").textContent = phase === "running" ? `剩餘 ${fmtSpan(rem)} · 共 ${fmtSpan(d)}` : `共 ${fmtSpan(d)}`;
    } else {
      prog.classList.toggle("indeterminate", phase === "running");
      bar.style.width = phase === "running" ? "100%" : "0";
      $("time-sub").textContent = "無限（手動停止）";
    }
  }

  function renderState(s) {
    const prevRun = state ? state.run_id : null;
    state = s;
    const running = s.phase === "running", stopping = s.phase === "stopping";
    const locked = running || stopping;

    // 圖卡標題列小字（原數值卡片副標）；警告標籤文字取自感測器狀態
    $("temp-note").textContent = s.enabled.temp && s.phase !== "idle" ? `${s.params.com_port} · 每秒更新` : "每秒更新";
    $("dist-note").textContent = `絕對距離 · 模式 ${s.params.refl_mode === "1" ? "1-鏡面反射" : "0-漫反射"}`;
    Object.entries(seriesCharts).forEach(([k, c]) => {
      if (!s.sensors[k]) return;
      const t = s.sensors[k].text;
      if (c.statusText !== t) { c.statusText = t; c.requestDraw(); }
    });

    renderTime(s.elapsed_int, s.elapsed, s.duration, s.phase);
    setDot($("card-time"), running ? "ok" : stopping ? "busy" : "idle");

    $("status-text").textContent = s.status_text;
    setDot($("card-status"), running ? "ok" : stopping ? "busy" : "idle");
    renderSensorPanel(s);

    $("btn-start").disabled = locked || busy;
    $("btn-stop").disabled = !running;
    $("btn-stop").textContent = stopping ? "儲存中…" : "停止監測";
    document.querySelectorAll("#settings input, #settings select, #settings .btn-icon")
      .forEach((el) => { el.disabled = locked; });


    // 模擬模式標示
    const sim = s.simulated || [];
    const simNames = { temp: "溫度", distance: "距離", audio: "音訊", spectrometer: "光譜儀", psu: "電源" };
    const chip = $("sim-chip");
    chip.hidden = !sim.length;
    chip.textContent = `模擬模式：${sim.map((k) => simNames[k] || k).join("、")}${s.simulate_faults ? "（含故障）" : ""}`;
    document.querySelectorAll("[data-sim]").forEach((el) => { el.hidden = !sim.includes(el.dataset.sim); });

    // 未啟用的感測器：圖卡顯示「未啟用」（尚未開始過監測時仍顯示「等待資料」）
    Object.entries(seriesCharts).forEach(([k, c]) => {
      if (!(k in s.enabled)) return;
      const off = s.phase !== "idle" && !s.enabled[k];
      if (c.disabled !== off) { c.disabled = off; c.requestDraw(); }
    });

    const optOff = s.phase !== "idle" && !s.enabled.spectrometer;
    if (optHeat.disabled !== optOff) { optHeat.disabled = optOff; optLive.disabled = optOff; optHeat.requestDraw(); optLive.requestDraw(); }
    const optText = s.sensors.spectrometer.text;
    if (optLive.statusText !== optText) { optLive.statusText = optText; optLive.requestDraw(); }

    if (prevRun !== null && s.run_id !== prevRun && s.run_id !== currentRun) {
      setupCharts(s.audio_meta, s.run_id);
    }
  }

  // ------------------------------------------------------------------
  // 通知與對話框
  // ------------------------------------------------------------------
  function toast(n) {
    if (n.id !== undefined) { if (seenNotices.has(n.id)) return; seenNotices.add(n.id); }
    const level = { info: "busy", success: "ok", warning: "warn", error: "error" }[n.level] || "idle";
    const el = document.createElement("div");
    el.className = "toast"; el.setAttribute("role", n.level === "error" ? "alert" : "status");
    el.innerHTML = '<span class="dot"></span><div><div class="toast-title"></div><div class="toast-msg"></div></div><button class="toast-close" aria-label="關閉">×</button>';
    el.querySelector(".dot").dataset.level = level;
    el.querySelector(".toast-title").textContent = n.title;
    el.querySelector(".toast-msg").textContent = n.message;
    el.querySelector(".toast-close").onclick = () => el.remove();
    $("toasts").appendChild(el);
    const ttl = { info: 6000, success: 12000, warning: 10000 }[n.level];
    if (ttl) setTimeout(() => el.remove(), ttl);
    while ($("toasts").children.length > 5) $("toasts").firstChild.remove();
  }

  function dialog({ title, lead, items = [], question = "", okText = "確定", cancelText = null }) {
    const dlg = $("dlg");
    $("dlg-title").textContent = title;
    $("dlg-lead").textContent = lead || ""; $("dlg-lead").hidden = !lead;
    const ul = $("dlg-list"); ul.innerHTML = "";
    items.forEach((t) => { const li = document.createElement("li"); li.textContent = t; ul.appendChild(li); });
    $("dlg-question").textContent = question; $("dlg-question").hidden = !question;
    $("dlg-ok").textContent = okText;
    $("dlg-cancel").hidden = !cancelText;
    if (cancelText) $("dlg-cancel").textContent = cancelText;
    dlg.returnValue = "";
    dlg.showModal();
    $("dlg-ok").focus();
    return new Promise((resolve) => {
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
    });
  }
  const showError = (items) => dialog({ title: "錯誤", items, okText: "確定" });

  // ------------------------------------------------------------------
  // 開始／停止
  // ------------------------------------------------------------------
  // ------------------------------------------------------------------
  // 實驗標籤視窗：按「輸入實驗標籤」開啟，填完按「開始監測」才進入預檢與開始流程
  // ------------------------------------------------------------------
  function specimenShape() {
    const r = document.querySelector('input[name="label_specimen_shape"]:checked');
    return r ? r.value : "square";
  }
  function setSpecimenShape(shape) {
    document.querySelectorAll('input[name="label_specimen_shape"]').forEach((r) => { r.checked = r.value === shape; });
    document.querySelectorAll("#labels-form .dims").forEach((d) => { d.hidden = d.dataset.shape !== shape; });
    $("specimen-hint").textContent = shape === "circle" ? "面積 mm²、厚度 mm" : "長 × 寬 × 厚，單位 mm";
  }
  function labelErrors() {
    const errs = LABEL_REQUIRED.filter(([k]) => !$(k).value.trim()).map(([, name]) => `${name}為必填`);
    SPECIMEN_DIMS[specimenShape()].forEach(([k, name]) => {
      const v = $(k).value.trim(), n = Number(v);
      if (!v) errs.push(`試片${name}為必填`);
      else if (!(Number.isFinite(n) && n > 0)) errs.push(`試片${name}必須是大於 0 的數字`);
    });
    return errs;
  }
  function openLabels() {
    if (busy || (state && (state.phase === "running" || state.phase === "stopping"))) return;
    $("labels-error").hidden = true;
    $("dlg-labels").showModal();
    $("label_metal").focus();
  }
  document.querySelectorAll('input[name="label_specimen_shape"]').forEach((r) =>
    r.addEventListener("change", () => setSpecimenShape(r.value)));
  // 正方形與圓形共用同一個厚度值（存檔 key 只有 label_specimen_thickness）
  $("label_specimen_thickness").addEventListener("input", (e) => { $("label_specimen_thickness_c").value = e.target.value; });
  $("label_specimen_thickness_c").addEventListener("input", (e) => { $("label_specimen_thickness").value = e.target.value; });
  $("labels-cancel").addEventListener("click", () => $("dlg-labels").close("cancel"));
  $("labels-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const errs = labelErrors();
    if (errs.length) {
      $("labels-error").textContent = errs.join("、");
      $("labels-error").hidden = false;
      return;
    }
    $("dlg-labels").close("ok");
    onStart();
  });

  async function onStart() {
    if (busy) return;
    busy = true; $("btn-start").disabled = true;
    try {
      const params = readForm();
      const pf = await api("/api/preflight", { method: "POST", body: params });
      if (!pf.data) { await showError(["預檢失敗：伺服器沒有回應"]); return; }
      if (pf.data.errors && pf.data.errors.length) { await showError(pf.data.errors); return; }
      if (pf.data.warnings && pf.data.warnings.length) {
        const go = await dialog({
          title: "警告", lead: "檢測到以下問題:", items: pf.data.warnings,
          question: "是否繼續執行監測？", okText: "繼續監測", cancelText: "取消",
        });
        if (!go) return;
      }
      const r = await api("/api/start", { method: "POST", body: { ...params, confirm: true } });
      if (!r.ok) {
        const msgs = (r.data && (r.data.errors || r.data.warnings)) || [`啟動監測失敗 (HTTP ${r.status})`];
        await showError(msgs);
        return;
      }
      $("labels-form").reset(); setSpecimenShape("square"); // 下一筆實驗從空白開始
      setupCharts(r.data.state.audio_meta, r.data.state.run_id);
      renderState(r.data.state);
    } catch (e) {
      await showError([`啟動監測失敗: ${e.message || e}`]);
    } finally {
      busy = false;
      if (state) renderState(state);
    }
  }

  async function onStop() {
    $("btn-stop").disabled = true;
    const r = await api("/api/stop", { method: "POST" });
    if (r.data && r.data.state) renderState(r.data.state);
    // 停止監測不會動電源輸出；輸出還開著就問要不要一併關掉（只有本機能關）
    const outputOn = psu.snap && ["relays", "powering", "on"].includes(psu.snap.phase);
    if (r.ok && outputOn) {
      const off = await dialog({
        title: "電源仍在輸出", lead: "監測已停止，但電源高壓輸出還開著。",
        question: psu.canControl ? "要現在關閉輸出嗎？" : "請到接電源的電腦關閉輸出，或按緊急關閉。",
        okText: psu.canControl ? "關閉輸出" : "知道了", cancelText: psu.canControl ? "保持輸出" : null,
      });
      if (off && psu.canControl) await psuAction("/api/psu/output", { on: false });
    }
  }

  // ------------------------------------------------------------------
  // 電源供應器（TruPlasma）：狀態由 SSE `psu` 事件推送，操作都走 /api/psu/*。
  // 控制只准伺服器本機（can_control）；緊急關閉任何裝置都可用。
  // ------------------------------------------------------------------
  const psu = { snap: null, table: [], modes: [], canControl: false, params: {}, limits: {}, spInit: false, onSince: null };
  const PSU_GROUPS = [["pulse", "脈衝與頻率"], ["arc", "電弧管理"], ["ignition", "點火"], ["comm", "通訊（唯讀）"]];
  const PSU_PHASE_TONE = { disconnected: "neutral", connecting: "warn", idle: "ok", relays: "warn",
    powering: "warn", on: "error", stopping: "warn" };
  let psuParamRows = {};   // channel → 該列的 DOM（寫入成功後只更新那一列）

  const fmtNum = (v, d) => (v === null || v === undefined ? "--" : Number(v).toFixed(d));
  const fmtParam = (v) => (v === null || v === undefined ? "—" : String(Number(Number(v).toPrecision(6))));
  const psuErrors = (r, fallback) => (r.data && r.data.errors) || [fallback];

  const recipeNumber = (id) => $(id).value.trim() === "" ? NaN : Number($(id).value);

  function recipeInputs() {
    const mono = document.querySelector('input[name="psu-kind"]:checked').value === "mono";
    return { mode: mono ? `mono_${document.querySelector('input[name="psu-pole"]:checked').value}` : "bipulse",
      freq_hz: recipeNumber("psu-freq"), duty_pct: recipeNumber("psu-duty"),
      u: recipeNumber("psu-sp-u"), i: recipeNumber("psu-sp-i"), p: recipeNumber("psu-sp-p") };
  }

  function currentRecipe() {
    const p = psu.params, sp = psu.snap.setpoints;
    return { mode: ({4: "mono_a", 8: "mono_b", 32: "bipulse"})[p[33]] || "未知",
      freq_hz: p[140] ? p[1100] : p[51130] * 1000, duty_pct: p[51131], ...sp };
  }

  function recipeChanged(key, a, b) {
    if (key === "mode") return a !== b;
    if (!Number.isFinite(a) || !Number.isFinite(b)) return true;
    return key === "freq_hz" ? Math.round(a * 10) !== Math.round(b * 10) : Math.abs(a - b) >= 1e-6;
  }

  function modeName(mode) {
    return ({ mono_a: "單極 A", mono_b: "單極 B", bipulse: "雙極" })[mode]
      || psu.modes[Math.log2(psu.params[33])] || "未知";
  }

  function recipeChanges(next, cur) {
    return Object.keys(next).filter((key) => recipeChanged(key, next[key], cur[key]));
  }

  function updateRecipe() {
    const r = recipeInputs(), bi = r.mode === "bipulse";
    $("psu-pole-box").hidden = bi;
    $("psu-freq-hint").textContent = bi ? "1000–50000 Hz" : "1–100000 Hz；1000 Hz 以下自動使用低頻模式";
    $("psu-duty-label").textContent = bi ? "正向 A 佔空比" : "佔空比";
    $("psu-duty-hint").textContent = bi && r.freq_hz > 0 ? `負向 B = ${100 - r.duty_pct} % · A ${(1000000 / r.freq_hz * r.duty_pct / 100).toFixed(1)} µs / B ${(1000000 / r.freq_hz * (100-r.duty_pct) / 100).toFixed(1)} µs（不含 off time）` : "";
    const cur = psu.snap && Object.keys(psu.params).length ? currentRecipe() : null;
    const note = $("psu-mode-note");
    note.hidden = !cur || cur.mode !== "未知" || psu.params[33] == null;
    note.textContent = note.hidden ? "" : `電源目前是 ${modeName(cur.mode)} 模式（不在單極／雙極選項），套用後會改成所選模式`;
    const fields = [
      ["freq", r.freq_hz, bi ? 1000 : 1, bi ? 50000 : 100000],
      ["duty", r.duty_pct, 1, 99],
      ...["u", "i", "p"].map((k) => ["sp-" + k, r[k], 0, psu.limits[k]])
    ];
    let valid = true;
    fields.forEach(([id, value, low, high]) => {
      const el = $("psu-" + id + "-validation");
      const invalid = !Number.isFinite(value) || !Number.isFinite(high) || value < low || value > high || (id === "sp-p" && value === 0);
      const warn = !invalid && (id === "sp-u" || id === "sp-i") && value === 0;
      const showValidation = !!(cur && psu.snap.connected);
      el.dataset.level = showValidation ? (invalid ? "error" : warn ? "warn" : "") : "";
      el.textContent = !showValidation ? "" : invalid ? (id === "sp-p" && value === 0 ? "功率上限必須大於 0，否則電源不會輸出" : Number.isFinite(high) ? `請輸入 ${low}–${high} 的數值` : "尚未取得設定範圍") : warn ? "為 0 時不會有輸出" : "";
      $("psu-" + id).setAttribute("aria-invalid", showValidation && invalid ? "true" : "false");
      valid = valid && !invalid;
    });
    const dirty = !!cur && recipeChanges(r, cur).length > 0;
    $("psu-dirty").hidden = !dirty;
    const apply = $("psu-apply"), enabled = dirty && valid && psu.canControl && psu.snap.phase === "idle";
    apply.disabled = !enabled;
    apply.classList.toggle("btn-start", enabled);
    apply.classList.toggle("btn-ghost", !enabled);
  }

  function fillRecipe() {
    if (!psu.snap || !Object.keys(psu.params).length) return;
    const r = currentRecipe();
    document.querySelector(`input[name="psu-kind"][value="${r.mode.startsWith("mono") ? "mono" : "bi"}"]`).checked = true;
    if (r.mode.startsWith("mono")) document.querySelector(`input[name="psu-pole"][value="${r.mode.slice(-1)}"]`).checked = true;
    $("psu-freq").value = r.freq_hz || ""; $("psu-duty").value = r.duty_pct ?? "";
    ["u", "i", "p"].forEach((k) => { $("psu-sp-" + k).value = k === "p" && r.p === 0 ? psu.limits.p_default : r[k]; });
    updateRecipe();
  }

  function renderPsu() {
    const sn = psu.snap;
    if (!sn) return;
    const st = sn.status, phase = sn.phase, ctl = psu.canControl, conn = sn.connected, idle = phase === "idle";
    if (phase === "on" && psu.onSince === null) psu.onSince = Date.now();
    if (phase !== "on") psu.onSince = null;
    // Test version 家族（61704／61711／61714）只是訊息：不顯示成紅色警報（docs/log-improvement-spec.md R3）
    const testMsg = !!(sn.alarm && sn.alarm.category === "test_version");
    const alarm = !!(st && st.alarm_active && !testMsg) || !!(sn.alarm && sn.alarm.level === "alarm");
    const secs = psu.onSince === null ? 0 : Math.floor((Date.now() - psu.onSince) / 1000);
    $("psu-phase").textContent = !conn ? "未連線" : alarm ? "警報" : phase === "on" ? `輸出中 ${String(Math.floor(secs/60)).padStart(2,"0")}:${String(secs%60).padStart(2,"0")}` : phase === "idle" ? "待機" : sn.phase_text;
    $("psu-phase").dataset.tone = alarm ? "error" : PSU_PHASE_TONE[phase] || "neutral";
    $("psu-panel").dataset.live = phase === "on" ? "1" : "0";
    renderPsuAge();
    let msg = "", level = "alarm";
    if (testMsg) { msg = `電源訊息 ${sn.alarm.code}（Test version 家族，只記錄）：${sn.alarm.raw || ""}`; level = "info"; }
    else if (sn.alarm) { msg = `${sn.alarm.level === "alarm" ? "警報" : "警告"} ${sn.alarm.code}：${sn.alarm.text}（原文：${sn.alarm.raw || "無"}）${sn.alarm.advice ? "：" + sn.alarm.advice : ""}`; level = sn.alarm.level; }
    else if (alarm) msg = "警報中（讀取警報代碼中…）";
    else if (st && st.warning_active) { msg = "警告中（讀取警報代碼中…）"; level = "warning"; }
    else if (sn.error && conn) { msg = sn.error; level = "warning"; }
    $("psu-banner").hidden = !msg; $("psu-banner").dataset.level = level; $("psu-banner-text").textContent = msg;
    $("psu-reset-alarms").hidden = !alarm && !(st && st.alarm_active);
    document.querySelectorAll("#psu-flags .psu-flag").forEach((el) => { el.dataset.on = st && st[el.dataset.flag] ? "1" : "0"; });
    const meas = sn.measurements || {}, mode = psu.params[33];
    for (const [pole, u, i] of [["a", "Uavg_A_V", "Iavg_A_A"], ["b", "Uavg_B_V", "Iavg_B_A"]]) {
      const inactive = (pole === "a" && mode === 8) || (pole === "b" && mode === 4);
      $("psu-col-" + pole).classList.toggle("inactive", inactive);
      $("psu-avg-u" + pole).textContent = inactive ? "—" : fmtNum(meas[u], 1);
      $("psu-avg-i" + pole).textContent = inactive ? "—" : fmtNum(meas[i], 2);
    }
    $("psu-summary").textContent = `總功率 ${fmtNum(st && st.p_act, 3)} kW · 調節 ${(st && st.regulation) || "—"} · 實際頻率 ${fmtNum(meas.Freq_Hz, 0)} Hz · 實際佔空比 ${fmtNum(meas.Duty_pct, 1)} % · 電弧 ${st ? st.arc_rate : "—"} 次/秒`;
    $("psu-u").textContent = fmtNum(st && st.u_act, 1); $("psu-i").textContent = fmtNum(st && st.i_act_peak, 2);
    $("psu-arc-counts").textContent = st && st.arc_counts ? Object.entries(st.arc_counts).map(([k,v]) => `${k} ${v}`).join(" · ") : "";
    const tb = $("psu-meas").tBodies[0], keys = Object.keys(meas).sort();
    if ([...tb.rows].map((r) => r.dataset.key).join() !== keys.join()) { tb.innerHTML = ""; keys.forEach((k) => { const tr = tb.insertRow(); tr.dataset.key = k; tr.insertCell().textContent = k; tr.insertCell(); }); }
    [...tb.rows].forEach((tr) => { tr.cells[1].textContent = meas[tr.dataset.key] ?? "--"; });
    $("psu-local-note").hidden = ctl; $("psu-locked").hidden = !conn || idle;
    $("psu-connect").hidden = conn; $("psu-disconnect").hidden = !conn;
    $("psu-port").disabled = !ctl || conn; $("psu-connect").disabled = !ctl || phase !== "disconnected"; $("psu-disconnect").disabled = !ctl || !conn;
    $("psu-settings").disabled = !ctl || !idle; $("psu-sp-p").disabled = !ctl || !idle;
    $("psu-read-params").disabled = !ctl || !conn; $("psu-reset-counters").disabled = !ctl || !conn;
    $("psu-on").disabled = !ctl || !idle; $("psu-off").disabled = !ctl || !["relays", "powering", "on"].includes(phase);
    $("psu-reset-alarms").disabled = !ctl || !idle; $("psu-estop").disabled = !conn;
    document.querySelectorAll("#psu-param-list [data-write]").forEach((el) => { el.disabled = !ctl || !idle; });
    updateRecipe();
  }

  function renderPsuAge() {
    const sn = psu.snap;
    if (!sn) return;
    const age = sn.last_ok ? fmtAgo(Math.max(0, Date.now() / 1000 - sn.last_ok)) : "";
    $("psu-meta").textContent = [sn.identity, sn.connected && age ? `最後正常 ${age}` : ""].filter(Boolean).join(" · ");
  }

  function psuParamRow(p) {
    const cur = psu.params[p.channel];
    const row = document.createElement("div");
    row.className = "psu-param";
    const name = document.createElement("div");
    name.className = "psu-param-name";
    name.textContent = `${p.name}  ch${p.channel}`;
    const val = document.createElement("div");
    val.className = "psu-param-val";
    val.textContent = `${fmtParam(cur)}${p.unit && cur !== undefined && cur !== null ? " " + p.unit : ""}`;
    row.append(name, val);
    if (p.writable) {
      let input;
      if (p.fmt === "mode" || p.fmt === "bool") {
        input = document.createElement("select");
        const opts = p.fmt === "mode" ? psu.modes.map((m, n) => [1 << n, `${n} ${m}`]) : [[0, "0（關）"], [1, "1（開）"]];
        opts.forEach(([v, label]) => input.add(new Option(label, v)));
        if (cur === undefined || cur === null || !opts.some(([v]) => v === cur)) input.add(new Option("—", ""), 0);
        input.value = cur === undefined || cur === null || !opts.some(([v]) => v === cur) ? "" : String(cur);
      } else {
        input = document.createElement("input");
        input.type = "number"; input.inputMode = "decimal";
        input.step = p.kind === "float" ? "any" : "1";
        input.min = p.fmt === "bits" ? 0 : (p.lo ?? ""); input.max = p.fmt === "bits" ? 255 : (p.hi ?? "");
        input.value = cur === undefined || cur === null ? "" : fmtParam(cur);
        if (p.lo !== null && p.hi !== null) input.placeholder = `${p.lo}–${p.hi}`;
      }
      input.dataset.write = "1";
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "btn btn-sm btn-ghost"; btn.textContent = "寫入"; btn.dataset.write = "1";
      btn.addEventListener("click", () => writePsuParam(p, input));
      const box = document.createElement("div");
      box.className = "psu-param-edit";
      box.append(input, btn);
      row.append(box);
    }
    if (p.note) {
      const note = document.createElement("div");
      note.className = "psu-param-note";
      note.textContent = p.note;
      row.append(note);
    }
    return row;
  }

  function renderPsuParams() {
    const list = $("psu-param-list");
    list.innerHTML = ""; psuParamRows = {};
    if (!Object.keys(psu.params).length) {
      list.innerHTML = '<p class="field-hint">尚未讀取：連線後按「讀取參數」。</p>';
      return;
    }
    PSU_GROUPS.forEach(([g, label]) => {
      const items = psu.table.filter((p) => p.group === g);
      if (!items.length) return;
      const h = document.createElement("h4");
      h.textContent = label;
      list.append(h);
      items.forEach((p) => { const row = psuParamRow(p); psuParamRows[p.channel] = row; list.append(row); });
    });
    renderPsu();
  }

  async function psuPost(path, body) {
    return api(path, { method: "POST", body: body || {} });
  }

  async function loadPsu() {
    const r = await api("/api/psu/state");
    if (!r.ok || !r.data) return;
    Object.assign(psu, { snap: r.data.snapshot, table: r.data.param_table, modes: r.data.pulse_modes,
      canControl: r.data.can_control, params: r.data.params || {}, limits: r.data.recipe_limits });
    ["u", "i", "p"].forEach((k) => { $("psu-sp-" + k).max = psu.limits[k]; });
    $("psu-sp-p").value = psu.limits.p_default;
    renderPsuParams();
    if (psu.snap.connected && psu.canControl) await onPsuReadParams();
    renderPsu();
  }

  async function psuAction(path, body) {
    const r = await psuPost(path, body);
    if (!r.ok) await showError(psuErrors(r, "電源操作失敗"));
    if (r.data && r.data.snapshot) { psu.snap = r.data.snapshot; renderPsu(); }
    return r;
  }

  async function onPsuConnect() {
    $("psu-connect").disabled = true;
    const r = await psuAction("/api/psu/connect", { port: $("psu-port").value.trim() || "COM8" });
    if (r.ok) {
      await onPsuReadParams();
    }
    renderPsu();
  }

  async function onPsuApply() {
    const next = recipeInputs(), cur = currentRecipe();
    const labels = { mode: "模式", freq_hz: "頻率 Hz", duty_pct: "佔空比 %", u: "電壓 V", i: "電流 A", p: "功率 kW" };
    const changes = recipeChanges(next, cur);
    if ($("psu-apply").disabled || !changes.length) return;
    const items = changes.map((k) => `${labels[k]}：${k === "mode" ? modeName(cur[k]) : (cur[k] ?? "—")} → ${k === "mode" ? modeName(next[k]) : next[k]}`);
    if (!await dialog({ title: "套用設定", lead: "變更將寫入電源。", items, question: "確定套用？", okText: "套用", cancelText: "取消" })) return;
    const r = await psuAction("/api/psu/recipe", next);
    if (r.ok) { psu.snap.setpoints = r.data.setpoints; psu.params = r.data.params; renderPsuParams(); fillRecipe(); renderPsu(); }
  }

  async function onPsuOn() {
    const sp = psu.snap.setpoints;
    // 開啟的是「電源目前已套用的設定值」；輸入框改了卻沒套用時不讓開，避免以為開的是新值
    if (!$("psu-dirty").hidden) {
      await showError(["輸入框的設定值還沒套用：請先按「套用設定值」再開啟輸出"]);
      return;
    }
    // 電源 CSV 只在實驗錄製中才寫；沒在錄就開輸出，這段電壓電流不會存檔也無法補錄
    const recording = !!(state && state.phase === "running");
    const items = [`電壓設定 ${sp.u} V`, `電流設定 ${sp.i} A`, `功率設定 ${sp.p} kW`];
    if (!recording) items.push("⚠ 目前沒有在錄製實驗：輸出期間的電壓電流不會存檔");
    const ok = await dialog({
      title: "開啟輸出", lead: "即將開啟高壓輸出（最高 1000 V / 25 A / 10 kW）。請確認電解槽與安全防護都已就緒。",
      items,
      question: recording ? "確定要開啟輸出？" : "沒有錄製也要開啟輸出？（要錄製請先取消，開始監測）",
      okText: recording ? "開啟輸出" : "仍然開啟輸出", cancelText: "取消",
    });
    if (ok) await psuAction("/api/psu/output", { on: true });
  }

  async function writePsuParam(p, input) {
    const raw = input.value;
    if (raw === "") { await showError([`${p.name}：請先輸入要寫入的值`]); return; }
    const cur = psu.params[p.channel];
    const show = (v) => (p.fmt === "mode" ? `${v}（${psu.modes[Math.log2(v)] ?? "?"}）` : fmtParam(v));
    const ok = await dialog({
      title: "寫入電源參數",
      lead: `${p.name} ch${p.channel}：${show(cur)} → ${show(Number(raw))}（會寫入電源 EEPROM）`,
      question: "確定要寫入？", okText: "寫入", cancelText: "取消",
    });
    if (!ok) return;
    const r = await psuPost("/api/psu/param", { channel: p.channel, value: Number(raw) });
    if (!r.ok) { await showError(psuErrors(r, "寫入失敗")); return; }
    psu.params = r.data.params || psu.params;
    const old = psuParamRows[p.channel], row = psuParamRow(p);
    if (old) old.replaceWith(row);
    psuParamRows[p.channel] = row;
    renderPsu();
  }

  async function onPsuReadParams() {
    const btn = $("psu-read-params");
    btn.disabled = true; btn.textContent = "讀取中…";
    try {
      const r = await psuPost("/api/psu/params/read");
      if (!r.ok) { await showError(psuErrors(r, "讀取參數失敗")); return; }
      psu.params = r.data.params || {};
      renderPsuParams();
      fillRecipe();
    } finally {
      btn.textContent = "讀取參數";
      renderPsu();
    }
  }

  // ------------------------------------------------------------------
  // 復原與 SSE
  // ------------------------------------------------------------------
  async function loadSpectrogramBulk() {
    const res = await fetch("/api/spectrogram", { cache: "no-store" });
    const start = parseInt(res.headers.get("X-Spec-Start"), 10) || 0;
    const count = parseInt(res.headers.get("X-Spec-Count"), 10) || 0;
    const buf = await res.arrayBuffer();
    if (count) specChart.addColumns(start, count, new Int16Array(buf));
  }

  async function sync() {
    syncing = true; pendingAudio = []; pendingSeries = []; pendingSpec = [];
    try {
      const { data: s } = await api("/api/state");
      const firstLoad = state === null;
      if (s.phase !== "idle") writeForm(s.params);
      setupCharts(s.audio_meta, s.run_id);
      renderState(s);
      if (s.spec_total > 0) await loadSpectrogramBulk();
      Object.entries(s.series || {}).forEach(([k, pts]) => seriesCharts[k].setAll(pts));
      restoreSpectrum(s.spectrum);
      const now = Date.now() / 1000;
      (s.notices || []).forEach((n) => {
        if (firstLoad && now - n.time > 30) { seenNotices.add(n.id); return; }
        toast(n);
      });
    } finally {
      syncing = false;
      const queued = pendingAudio; pendingAudio = [];
      queued.forEach(applyAudio);
      const queuedSeries = pendingSeries; pendingSeries = [];
      queuedSeries.forEach(applySeries);
      const queuedSpec = pendingSpec; pendingSpec = [];
      queuedSpec.forEach(([kind, msg]) => (kind === "meta" ? applySpecMeta(msg) : applySpecCol(msg)));
    }
  }
  let resyncTimer = 0;
  function resync() {
    if (resyncTimer) return;
    resyncTimer = setTimeout(() => { resyncTimer = 0; sync(); }, 100);
  }

  function connect() {
    const es = new EventSource("/api/stream");
    const chip = $("conn-chip");
    let wasOpen = false;
    es.onopen = () => {
      setDot(chip, "ok"); $("conn-text").textContent = "已連線";
      if (wasOpen) { resync(); loadPsu(); } // 斷線重連：重新復原
      wasOpen = true;
    };
    es.onerror = () => { setDot(chip, "error"); $("conn-text").textContent = "重新連線中"; };
    es.addEventListener("state", (e) => { if (!syncing) renderState(JSON.parse(e.data)); });
    es.addEventListener("tick", (e) => {
      if (!state || state.phase !== "running") return;
      const t = JSON.parse(e.data);
      state.elapsed = t.elapsed; state.elapsed_int = t.elapsed_int;
      renderTime(t.elapsed_int, t.elapsed, t.duration, state.phase);
    });
    es.addEventListener("audio", (e) => {
      const msg = JSON.parse(e.data);
      if (syncing) pendingAudio.push(msg); else applyAudio(msg);
    });
    es.addEventListener("series", (e) => {
      const msg = JSON.parse(e.data);
      if (syncing) pendingSeries.push(msg); else applySeries(msg);
    });
    es.addEventListener("spec_meta", (e) => {
      const msg = JSON.parse(e.data);
      if (syncing) pendingSpec.push(["meta", msg]); else applySpecMeta(msg);
    });
    es.addEventListener("spectrum", (e) => {
      const msg = JSON.parse(e.data);
      if (syncing) pendingSpec.push(["col", msg]); else applySpecCol(msg);
    });
    es.addEventListener("psu", (e) => { psu.snap = JSON.parse(e.data); renderPsu(); });
    es.addEventListener("toast", (e) => toast(JSON.parse(e.data)));
    es.addEventListener("resync", () => resync()); // 伺服器端佇列塞滿（例如分頁在背景太久）時要求重新同步
    es.addEventListener("reset", (e) => {
      const r = JSON.parse(e.data);
      if (r.run_id !== currentRun) setupCharts(r.audio_meta, r.run_id);
    });
  }

  // ------------------------------------------------------------------
  // 初始化
  // ------------------------------------------------------------------
  $("btn-start").addEventListener("click", openLabels);
  $("btn-stop").addEventListener("click", onStop);
  $("refresh-com").addEventListener("click", () => loadCom(false));
  $("refresh-audio").addEventListener("click", () => loadAudio(true));
  setInterval(renderSensorAgo, 1000); // 狀態面板的「最後正常 N 秒前」每秒更新（含停止後）
  setInterval(renderPsuAge, 1000);
  $("psu-connect").addEventListener("click", onPsuConnect);
  $("psu-disconnect").addEventListener("click", () => psuAction("/api/psu/disconnect"));
  document.querySelectorAll('#psu-settings input, #psu-sp-p').forEach((el) => el.addEventListener("input", () => {
    updateRecipe();
  }));
  $("psu-apply").addEventListener("click", onPsuApply);
  $("psu-on").addEventListener("click", onPsuOn);
  $("psu-off").addEventListener("click", () => psuAction("/api/psu/output", { on: false }));
  $("psu-reset-alarms").addEventListener("click", () => psuAction("/api/psu/reset-alarms"));
  $("psu-reset-counters").addEventListener("click", () => psuAction("/api/psu/reset-counters"));
  $("psu-estop").addEventListener("click", () => psuAction("/api/psu/estop"));
  $("psu-read-params").addEventListener("click", onPsuReadParams);

  (async () => {
    connect();
    await Promise.all([loadCom(false), loadAudio(false)]);
    await sync();
    loadPsu();
  })();
})();
