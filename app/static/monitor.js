/* 感測器整合系統：網頁版前端。無外部函式庫，圖表以 <canvas> 繪製。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PARAM_KEYS = ["com_port", "audio_device", "sample_rate",
    "update_interval", "history_duration", "refl_mode", "distance_interval"];
  const AUDIO_PLACEHOLDERS = ["無可用音訊設備", "音訊設備檢測失敗"];

  let theme = readTheme();
  let state = null;          // 最近一次伺服器狀態
  let currentRun = null;     // 目前圖表所屬的 run_id
  let busy = false;          // 開始流程進行中
  let syncing = true;        // 正在從 /api/state 復原
  let pendingAudio = [];     // 復原期間暫存的 SSE 音訊事件
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
      wave: v("--chart-wave"), fft: v("--chart-fft"),
      font: v("--font"), mono: v("--mono"),
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
  // 折線圖（波形／頻譜）
  // ------------------------------------------------------------------
  class LineChart extends CanvasChart {
    constructor(canvas, opts) {
      super(canvas, { l: 62, r: 14, t: 8, b: 38 });
      this.opts = opts;
      this.data = null;
      this.yr = null;
      this.empty = "等待資料";
    }
    clear(msg) { this.data = null; this.yr = null; this.empty = msg || "等待資料"; this.requestDraw(); }
    set(x0, x1, ys) {
      this.data = { x0, x1, ys };
      let lo = Infinity, hi = -Infinity;
      for (const v of ys) { if (v < lo) lo = v; if (v > hi) hi = v; }
      const target = this.opts.range(lo, hi);
      if (!this.yr) this.yr = target;
      else {
        // 擴張立即、收縮緩慢，避免軸不停跳動
        const [a, b] = this.yr;
        this.yr = [target[0] < a ? target[0] : a + (target[0] - a) * 0.15,
                   target[1] > b ? target[1] : b + (target[1] - b) * 0.15];
      }
      this.requestDraw();
    }
    draw() {
      const p = this.begin();
      const d = this.data;
      const x = d ? [d.x0, d.x1] : this.opts.defaultX();
      const y = this.yr || this.opts.defaultY;
      const { X, Y } = this.axes(p, x, y, this.opts.labels);
      if (!d || !d.ys.length) { this.placeholder(p, this.empty); return; }
      const ctx = this.ctx;
      ctx.save();
      ctx.beginPath(); ctx.rect(p.x, p.y, p.w, p.h); ctx.clip();
      ctx.strokeStyle = this.opts.color();
      ctx.lineWidth = 1.25; ctx.lineJoin = "round";
      ctx.beginPath();
      const n = d.ys.length;
      const dx = n > 1 ? (d.x1 - d.x0) / (n - 1) : 0;
      for (let i = 0; i < n; i++) {
        const px = X(d.x0 + dx * i), py = Y(d.ys[i]);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.stroke();
      ctx.restore();
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
    colorbar(p) {
      const ctx = this.ctx;
      const bx = p.x + p.w + 14, bw = 12, by = p.y, bh = p.h;
      for (let i = 0; i < bh; i++) {
        const li = Math.round((1 - i / Math.max(1, bh - 1)) * 255) * 3;
        ctx.fillStyle = `rgb(${JET[li]},${JET[li + 1]},${JET[li + 2]})`;
        ctx.fillRect(bx, by + i, bw, 1.5);
      }
      ctx.strokeStyle = theme.axis;
      ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, bh - 1);
      const yt = ticks(this.vmin, this.vmax, Math.max(3, Math.floor(bh / 45)));
      const step = yt.length > 1 ? yt[1] - yt[0] : 1;
      ctx.fillStyle = theme.tick; ctx.font = `11px ${theme.mono}`;
      ctx.textAlign = "left"; ctx.textBaseline = "middle";
      yt.forEach((v) => {
        const py = by + bh - (v - this.vmin) / (this.vmax - this.vmin) * bh;
        ctx.fillRect(bx + bw, py, 3, 1);
        ctx.fillText(fmtTick(v, step), bx + bw + 5, py);
      });
      ctx.save();
      ctx.fillStyle = theme.text; ctx.font = `11.5px ${theme.font}`;
      ctx.translate(this.w - 8, by + bh / 2); ctx.rotate(-Math.PI / 2);
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText("Amplitude (dB)", 0, 0);
      ctx.restore();
    }
  }

  const specChart = new Spectrogram($("cv-spec"));
  const waveChart = new LineChart($("cv-wave"), {
    labels: ["Time (s)", "Amplitude"],
    color: () => theme.wave,
    defaultX: () => [0, parseFloat($("update_interval").value) || 0.1],
    defaultY: [-1000, 1000],
    range: (lo, hi) => { const a = Math.max(Math.abs(lo), Math.abs(hi), 50) * 1.1; return [-a, a]; },
  });
  const fftChart = new LineChart($("cv-fft"), {
    labels: ["Frequency (Hz)", "Magnitude (dB)"],
    color: () => theme.fft,
    defaultX: () => [0, (parseFloat($("sample_rate").value) || 22050) / 2],
    defaultY: [0, 120],
    range: (lo, hi) => { const pad = Math.max(3, (hi - lo) * 0.06); return [lo - pad, hi + pad]; },
  });
  const charts = [specChart, waveChart, fftChart];

  function applyAudio(msg) {
    if (msg.run_id !== currentRun) return;
    if (msg.wave) {
      waveChart.set(0, msg.wave.n > 1 ? (msg.wave.n - 1) / (state?.audio_meta?.sample_rate || 22050) : 0, msg.wave.y);
      $("wave-note").textContent = `${msg.wave.n} samples · ${(msg.wave.duration * 1000).toFixed(0)} ms`;
    }
    if (msg.spectrum) {
      fftChart.set(msg.spectrum.fmin, msg.spectrum.fmax, msg.spectrum.y);
      $("fft-note").textContent = `${msg.spectrum.y.length} bins · 0–${Math.round(msg.spectrum.fmax)} Hz`;
    }
    if (msg.spec && msg.spec.count) {
      const bin = atob(msg.spec.b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const arr = new Int16Array(bytes.buffer);
      if (msg.spec.start > specChart.total) { resync(); return; } // 有漏欄：重新抓整段
      specChart.addColumns(msg.spec.start, msg.spec.count, arr);
    }
  }

  function setupCharts(meta, runId) {
    currentRun = runId;
    specChart.configure(meta);
    $("spec-note").textContent =
      `NFFT ${meta.nfft} · overlap ${meta.noverlap} · ${meta.sample_rate} Hz · 顯示 ${meta.history_duration} 秒`;
    waveChart.clear(); fftChart.clear();
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
    return out;
  }
  function writeForm(params) {
    PARAM_KEYS.forEach((k) => {
      if (params[k] === undefined) return;
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

  function renderValue(numEl, unitEl, sensor, unit) {
    const blank = sensor.text === `-- ${unit}`;
    if (sensor.value !== null && sensor.value !== undefined) {
      numEl.textContent = Number(sensor.value).toFixed(1); numEl.classList.remove("is-text"); unitEl.hidden = false;
    } else if (blank) {
      numEl.textContent = "--"; numEl.classList.remove("is-text"); unitEl.hidden = false;
    } else {
      numEl.textContent = sensor.text; numEl.classList.add("is-text"); unitEl.hidden = true;
    }
  }

  function renderTime(elapsedInt, elapsed, duration, phase) {
    $("time-num").textContent = String(elapsedInt);
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
      $("time-sub").textContent = phase === "running" ? `剩餘 ${rem} 秒 · 共 ${d} 秒` : `共 ${d} 秒`;
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

    renderValue($("temp-num"), $("temp-unit"), s.sensors.temp, "°C");
    renderValue($("dist-num"), $("dist-unit"), s.sensors.distance, "mm");
    setDot($("card-temp"), s.sensors.temp.level);
    setDot($("card-distance"), s.sensors.distance.level);
    $("temp-sub").textContent = s.enabled.temp && s.phase !== "idle" ? `${s.params.com_port} · 每秒更新` : "每秒更新";
    $("dist-sub").textContent = `絕對距離 · 模式 ${s.params.refl_mode === "1" ? "1-鏡面反射" : "0-漫反射"}`;

    renderTime(s.elapsed_int, s.elapsed, s.duration, s.phase);
    setDot($("card-time"), running ? "ok" : stopping ? "busy" : "idle");

    $("status-text").textContent = s.status_text;
    setDot($("card-status"), running ? "ok" : stopping ? "busy" : "idle");
    setDot($("lamp-temp"), s.sensors.temp.level);
    setDot($("lamp-audio"), s.sensors.audio.level);
    setDot($("lamp-distance"), s.sensors.distance.level);
    $("audio-sub").textContent = `音訊：${s.sensors.audio.text}`;

    $("btn-start").disabled = locked || busy;
    $("btn-stop").disabled = !running;
    $("btn-stop").textContent = stopping ? "儲存中…" : "停止監測";
    document.querySelectorAll("#settings input, #settings select, #settings .btn-icon")
      .forEach((el) => { el.disabled = locked; });

    renderRecording(s);

    // 模擬模式標示
    const sim = s.simulated || [];
    const simNames = { temp: "溫度", distance: "距離", audio: "音訊" };
    const chip = $("sim-chip");
    chip.hidden = !sim.length;
    chip.textContent = `模擬模式：${sim.map((k) => simNames[k] || k).join("、")}${s.simulate_faults ? "（含故障）" : ""}`;
    document.querySelectorAll("[data-sim]").forEach((el) => { el.hidden = !sim.includes(el.dataset.sim); });

    if (prevRun !== null && s.run_id !== prevRun && s.run_id !== currentRun) {
      setupCharts(s.audio_meta, s.run_id);
    }
  }

  // ------------------------------------------------------------------
  // 存檔資訊列：資料夾與各檔案大小／筆數
  // ------------------------------------------------------------------
  function fmtBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
    return `${(n / 1024 ** 3).toFixed(2)} GB`;
  }
  function fmtDuration(sec) {
    const t = Math.floor(sec), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), ss = t % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${m}:${String(ss).padStart(2, "0")}`;
  }
  function renderRecording(s) {
    const bar = $("recbar"), rec = s.recording;
    if (!rec) { bar.hidden = true; return; }
    bar.hidden = false;
    const job = s.spectrogram_job;
    let title = "資料存檔中", level = "ok";
    if (s.phase === "stopping") {
      title = job && job.status === "running"
        ? `頻譜 CSV 產生中… ${Math.round((job.progress || 0) * 100)}%（原始資料已安全落地）` : "關閉檔案中…";
      level = "busy";
    } else if (s.phase !== "running") { title = "本次實驗資料"; level = "idle"; }
    $("rec-title").textContent = title;
    $("rec-dot").dataset.level = level;
    $("rec-dir").textContent = rec.dir;
    const ul = $("rec-files"); ul.innerHTML = "";
    rec.files.forEach((f) => {
      const li = document.createElement("li");
      const name = document.createElement("span"); name.className = "fname"; name.textContent = f.name; name.title = f.name;
      const stat = document.createElement("span"); stat.className = "fstat";
      let detail = fmtBytes(f.bytes || 0);
      if (f.kind === "wav") detail += ` · ${fmtDuration(f.seconds || 0)} · ${f.channels} ch`;
      else if (f.kind === "csv") detail += ` · ${f.rows} 筆`;
      else if (f.rows) detail += ` · ${f.rows} 列`;
      stat.textContent = detail;
      li.append(name, stat);
      if (f.write_error) {
        const e = document.createElement("span"); e.className = "ferr"; e.textContent = "寫入失敗"; e.title = f.write_error;
        li.append(e);
      }
      ul.appendChild(li);
    });
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
    syncing = true; pendingAudio = [];
    try {
      const { data: s } = await api("/api/state");
      const firstLoad = state === null;
      if (s.phase !== "idle") writeForm(s.params);
      setupCharts(s.audio_meta, s.run_id);
      renderState(s);
      if (s.spec_total > 0) await loadSpectrogramBulk();
      if (s.wave || s.spectrum) applyAudio({ run_id: s.run_id, wave: s.wave, spectrum: s.spectrum });
      const now = Date.now() / 1000;
      (s.notices || []).forEach((n) => {
        if (firstLoad && now - n.time > 30) { seenNotices.add(n.id); return; }
        toast(n);
      });
    } finally {
      syncing = false;
      const queued = pendingAudio; pendingAudio = [];
      queued.forEach(applyAudio);
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
      if (wasOpen) resync(); // 斷線重連：重新復原
      wasOpen = true;
    };
    es.onerror = () => { setDot(chip, "error"); $("conn-text").textContent = "重新連線中"; };
    es.addEventListener("state", (e) => { if (!syncing) renderState(JSON.parse(e.data)); });
    es.addEventListener("tick", (e) => {
      if (!state || state.phase !== "running") return;
      const t = JSON.parse(e.data);
      state.elapsed = t.elapsed; state.elapsed_int = t.elapsed_int;
      if (t.recording) { state.recording = t.recording; renderRecording(state); }
      renderTime(t.elapsed_int, t.elapsed, t.duration, state.phase);
    });
    es.addEventListener("audio", (e) => {
      const msg = JSON.parse(e.data);
      if (syncing) pendingAudio.push(msg); else applyAudio(msg);
    });
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
  $("btn-start").addEventListener("click", onStart);
  $("btn-stop").addEventListener("click", onStop);
  $("refresh-com").addEventListener("click", () => loadCom(false));
  $("refresh-audio").addEventListener("click", () => loadAudio(true));
  ["sample_rate", "update_interval"].forEach((k) =>
    $(k).addEventListener("input", () => { waveChart.requestDraw(); fftChart.requestDraw(); }));

  (async () => {
    connect();
    await Promise.all([loadCom(false), loadAudio(false)]);
    await sync();
  })();
})();
