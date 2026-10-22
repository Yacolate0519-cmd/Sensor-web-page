// 溫度計校正頁（RiXEN TX-600N）：A 穩定偏差、B 反應速度 τ、C PEO 實驗記錄、D 匯出。
// 讀值由伺服器 /api/temp-cal/state 提供；標籤、事件、校正結果只存在瀏覽器（localStorage，以讀取輪次 session 為 key），
// 匯出時再把讀值和這些時間戳紀錄對起來。
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const TAU_FRACTION = 1 - Math.exp(-1);  // 63.2%
  const SPIKE_C = 2, SPIKE_DT = 1.5;       // 1 秒內（容許取樣誤差到 1.5 秒）變化 > 2°C 視為跳動異常
  const ALARM_HOLD_S = 15;                 // 跳動警告顯示多久
  const CUP_LABEL = { A: "A 杯（室溫）", B: "B 杯（約 50°C）" };
  const PROBE_LABEL = { bare: "裸測棒", sleeved: "套膜" };
  const PHASE_LABEL = { A: "A 穩定偏差", B: "B 反應速度", C: "C PEO 記錄" };

  // ---------- 狀態 ----------
  // readings: [t, T1|null, T2|null, T1 錯誤說明, T2 錯誤說明, [rawT1, rawT2, status]|null]
  const readings = [];
  let next = 0, running = false, session = 0, timer = null;
  let log = null;       // 本輪的標記（見 newLog）
  let alarm = null;     // {text, sub, until}
  let lastSpikeIdx = -1;

  function newLog(t0) {
    return {
      session: t0,
      ctx: [{ t: t0, phase: "A", cup: "A", probe: "bare" }],  // 標籤變更紀錄（依時間排序）
      events: [],      // {t, kind, text}
      devs: [],        // A：{t, cup, probe, glass, mean, std, n, stable}
      trials: [],      // B：{t0, cup0, cup1, probe, start, end, endSrc, target, tau, tEnd}
      step: null,      // B 進行中的階躍：{t0, start, cup0, cup1, probe}
      peo: null,       // C 進行中：{t}
      settings: {},
    };
  }

  const store = {
    load(s) { try { return JSON.parse(localStorage.getItem(`tempcal:${s}`)); } catch { return null; } },
    save() {
      if (!log) return;
      try { localStorage.setItem(`tempcal:${log.session}`, JSON.stringify(log)); } catch { /* 無法存就只留在記憶體 */ }
    },
    dropOthers(s) {
      try {
        for (let i = localStorage.length - 1; i >= 0; i--) {
          const k = localStorage.key(i);
          if (k && k.startsWith("tempcal:") && k !== `tempcal:${s}`) localStorage.removeItem(k);
        }
      } catch { /* ignore */ }
    },
  };

  // ---------- 小工具 ----------
  const fmt = (v, d = 2) => (v == null || !isFinite(v) ? "--" : v.toFixed(d));
  const sgn = (v, d = 2) => (v == null || !isFinite(v) ? "--" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d));
  const pad = (n) => String(n).padStart(2, "0");
  const timeStr = (t) => { const d = new Date(t * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
  const isoStr = (t) => { const d = new Date(t * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${timeStr(t)}.${String(d.getMilliseconds()).padStart(3, "0")}`; };
  const durStr = (s) => (s == null || !isFinite(s) ? "--" : `${Math.floor(s / 60)}:${pad(Math.floor(s % 60))}`);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const now = () => Date.now() / 1000;
  const lastT = () => (readings.length ? readings[readings.length - 1][0] : now());
  const ch = () => +$("channel").value;
  const val = (r) => r[ch()];
  const errOf = (r) => r[2 + ch()];
  const num = (id) => { const v = parseFloat($(id).value); return isFinite(v) ? v : null; };
  const winS = () => Math.max(10, num("win") || 60);
  const thC = () => { const v = num("th"); return v == null ? 0.1 : v; };
  const soakS = () => Math.max(0, (num("soak") ?? 5) * 60);
  const mean = (a) => a.reduce((x, y) => x + y, 0) / a.length;
  const sd = (a) => { if (a.length < 2) return 0; const m = mean(a); return Math.sqrt(a.reduce((x, y) => x + (y - m) ** 2, 0) / (a.length - 1)); };

  // 第一筆 t 大於 x 的索引
  function upper(x) {
    let lo = 0, hi = readings.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (readings[m][0] <= x) lo = m + 1; else hi = m; }
    return lo;
  }

  // ---------- 標籤（ctx） ----------
  function ctxAt(t) {
    let c = log.ctx[0];
    for (const e of log.ctx) { if (e.t <= t) c = e; else break; }
    return c;
  }
  function ctxNow() { return log.ctx[log.ctx.length - 1]; }
  function pushCtx(patch) {
    const c = { ...ctxNow(), ...patch, t: Math.max(now(), ctxNow().t) };
    log.ctx.push(c);
    store.save();
  }
  // 目前這組（杯子＋測棒狀態）從什麼時候開始
  function comboStart(t) {
    let key = null, start = log.ctx[0].t;
    for (const e of log.ctx) {
      if (e.t > t) break;
      const k = e.cup + e.probe;
      if (k !== key) { key = k; start = e.t; }
    }
    return start;
  }

  // ---------- 統計 ----------
  function statsAt(endT, win) {
    const vals = []; let fails = 0;
    for (let i = upper(endT) - 1; i >= 0 && readings[i][0] > endT - win; i--) {
      const v = val(readings[i]);
      if (v == null) fails++; else vals.push(v);
    }
    if (!vals.length) return { n: 0, fails, win };
    const min = Math.min(...vals), max = Math.max(...vals);
    return { n: vals.length, fails, win, mean: mean(vals), std: sd(vals), min, max, range: max - min,
      full: vals.length >= win * 0.8 };  // 視窗內至少八成秒數有讀值才判定
  }
  // 線性回歸斜率（°C/s），點數不足一半視窗時回 null
  function slopeAt(endT, win) {
    let n = 0, sx = 0, sy = 0, sxx = 0, sxy = 0;
    for (let i = upper(endT) - 1; i >= 0 && readings[i][0] > endT - win; i--) {
      const v = val(readings[i]); if (v == null) continue;
      const x = readings[i][0] - endT;
      n++; sx += x; sy += v; sxx += x * x; sxy += x * v;
    }
    if (n < Math.max(3, win * 0.5)) return null;
    const d = n * sxx - sx * sx;
    return d ? (n * sxy - sx * sy) / d : null;
  }
  // 穩定判定：{ok, label, s}
  function stableAt(t) {
    const s = statsAt(t, winS());
    if (!s.n) return { ok: false, label: "資料不足", state: "none", s };
    if (!s.full) return { ok: false, label: `收集中 ${s.n}/${Math.round(s.win * 0.8)}`, state: "none", s };
    if (s.range > thC() + 1e-9) return { ok: false, label: "尚未穩定", state: "no", s };
    const c = ctxAt(t);
    if (c.probe === "sleeved") {
      const soaked = t - comboStart(t);
      if (soaked < soakS()) return { ok: false, label: `套膜浸泡中 ${durStr(soaked)}/${durStr(soakS())}`, state: "no", s };
    }
    return { ok: true, label: "穩定", state: "yes", s };
  }

  // ---------- τ ----------
  function tauMeans() {
    const by = { up: [], down: [] };
    for (const tr of log.trials) if (tr.tau != null) by[tr.end > tr.start ? "up" : "down"].push(tr.tau);
    const all = [...by.up, ...by.down];
    return { up: by.up, down: by.down, all: all.length ? mean(all) : null };
  }
  function tauUsed() {
    const v = num("tau-use");
    if (v != null) return v > 0 ? v : null;
    return tauMeans().all;
  }
  // 讀值第一次越過 target 的時間（線性內插）
  function crossTime(t0, start, target) {
    const dir = Math.sign(target - start);
    let pt = t0, pv = start;
    for (let i = upper(t0); i < readings.length; i++) {
      const v = val(readings[i]); if (v == null) continue;
      const t = readings[i][0];
      if ((v - target) * dir >= 0) return pv === v ? t : pt + (target - pv) / (v - pv) * (t - pt);
      pt = t; pv = v;
    }
    return null;
  }

  // ---------- 伺服器 ----------
  async function loadCom() {
    $("refresh-com").classList.add("spinning");
    try {
      const data = (await (await fetch("/api/devices?kind=com")).json()).com;
      const list = $("com-list"); list.innerHTML = "";
      for (const p of data.ports) {
        const o = document.createElement("option");
        o.value = p.device; o.label = p.description; list.appendChild(o);
      }
      $("com-hint").textContent = data.ports.length
        ? `偵測到 ${data.ports.length} 個 COM 埠；留空＝自動偵測。溫度計要停在即時量測畫面` : "目前沒有 COM 埠";
    } catch { $("com-hint").textContent = "讀取 COM 清單失敗"; }
    $("refresh-com").classList.remove("spinning");
  }

  async function start() {
    $("msg").textContent = "";
    if (readings.length && !confirm("開始新一輪會清掉目前畫面上的讀值和標記。已經匯出 CSV 了嗎？")) return;
    $("btn-start").disabled = true;
    $("btn-start").textContent = "連線中…";
    try {
      const r = await fetch("/api/temp-cal/start", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ com_port: $("com_port").value }) });
      const body = await r.json();
      if (!body.ok) $("msg").textContent = (body.errors || ["無法開始"]).join("\n");
    } catch { $("msg").textContent = "無法連線到伺服器"; }
    $("btn-start").textContent = "開始讀取";
    poll();
  }
  async function stop() {
    if (log && log.peo && !confirm("PEO 記錄還在進行中，確定要停止讀取？")) return;
    $("btn-stop").disabled = true;
    try { await fetch("/api/temp-cal/stop", { method: "POST" }); } catch { /* 下一次 poll 會更新狀態 */ }
    poll();
  }

  function adoptSession(s) {
    session = s;
    readings.length = 0; next = 0; alarm = null; lastSpikeIdx = -1;
    log = (s && store.load(s)) || newLog(s || now());
    if (s) { store.dropOthers(s); store.save(); }
    applyLogToInputs();
  }

  async function poll() {
    clearTimeout(timer);
    try {
      const s = await (await fetch(`/api/temp-cal/state?since=${next}`)).json();
      if (s.session !== session) {  // 新的一輪（或頁面剛打開）：從頭取
        adoptSession(s.session);
        timer = setTimeout(poll, 0);
        return;
      }
      const before = readings.length;
      for (const p of s.readings) readings.push(p);
      next = s.next;
      running = s.running;
      checkAlarms(before);
      $("sim-chip").hidden = !s.simulated;
      $("status-dot").dataset.level = s.status.level;
      $("status-text").textContent = running ? `${s.status.text} · ${s.com_port}` : s.status.text;
      const dev = s.device;
      $("device-chip").hidden = !dev;
      if (dev) $("device-chip").textContent = `${dev.tc_type} 型 · ${dev.unit_f ? "°F" : "°C"} · 電量 ${["0–24", "25–49", "50–74", "75–100"][dev.battery]}%`;
      $("btn-start").disabled = running;
      $("btn-stop").disabled = !running;
      $("com_port").disabled = running;
      render();
    } catch {
      $("status-dot").dataset.level = "error";
      $("status-text").textContent = "伺服器未連線";
    }
    timer = setTimeout(poll, running ? 1000 : 3000);
  }

  // ---------- 異常警告 ----------
  function prevValid(i) {
    for (let j = i - 1; j >= 0; j--) if (val(readings[j]) != null) return j;
    return -1;
  }
  function isSpike(i) {
    const v = val(readings[i]); if (v == null) return false;
    const j = prevValid(i); if (j < 0) return false;
    return readings[i][0] - readings[j][0] <= SPIKE_DT && Math.abs(v - val(readings[j])) > SPIKE_C;
  }
  function checkAlarms(from) {
    for (let i = Math.max(from, 1); i < readings.length; i++) {
      if (isSpike(i) && i !== lastSpikeIdx) {
        lastSpikeIdx = i;
        const d = val(readings[i]) - val(readings[prevValid(i)]);
        alarm = { text: `讀值跳動異常：1 秒內變化 ${sgn(d, 1)} °C`, sub: `${timeStr(readings[i][0])}，可能是雜訊、漏電或接觸不良`, until: readings[i][0] + ALARM_HOLD_S };
      }
    }
  }
  function renderAlarm(last) {
    const box = $("alarm");
    let text = null, sub = "";
    if (last && val(last) == null) { text = `T${ch()} 讀值異常`; sub = errOf(last); }
    else if (alarm && lastT() <= alarm.until) { text = alarm.text; sub = alarm.sub; }
    box.hidden = !text;
    if (text) box.innerHTML = `⚠ ${esc(text)}<small>${esc(sub)}</small>`;
  }

  // ---------- 畫面 ----------
  function render() {
    if (!log) return;
    const last = readings[readings.length - 1];
    const other = ch() === 1 ? 2 : 1;
    $("cur-title").textContent = `T${ch()} 目前溫度`;
    if (!last) {
      $("current").innerHTML = "--<small>°C</small>"; $("current").classList.remove("bad");
      $("other-ch").textContent = `T${other} --`; $("last-time").textContent = "尚未讀取"; $("err-detail").textContent = "";
    } else {
      const v = val(last);
      $("current").innerHTML = `${v == null ? "Err" : fmt(v, 1)}<small>°C</small>`;
      $("current").classList.toggle("bad", v == null);
      const ov = last[other];
      $("other-ch").textContent = `T${other} ${ov == null ? "Err" : fmt(ov, 1) + " °C"}`;
      $("last-time").textContent = `最後一筆 ${timeStr(last[0])}`;
      $("err-detail").textContent = v == null ? errOf(last) : "";
    }
    renderAlarm(last);
    $("rec-flag").hidden = !log.peo;

    const t = lastT();
    const st = stableAt(t);
    const s = st.s;
    $("stats-title").textContent = `最近 ${winS()} 秒 · ${CUP_LABEL[ctxNow().cup]} · ${PROBE_LABEL[ctxNow().probe]}`;
    $("s-mean").textContent = fmt(s.mean, 3);
    $("s-std").textContent = fmt(s.std, 3);
    $("s-range").textContent = fmt(s.range, 3);
    const slope = slopeAt(t, +$("slope-win").value);
    $("s-slope").textContent = slope == null ? "--" : `${sgn(slope * 60, 2)}/分`;
    $("s-soak").textContent = readings.length ? durStr(t - comboStart(t)) : "--";
    $("s-n").textContent = s.n || s.fails ? `${s.n}／${s.fails}` : "--";
    const badge = $("stable");
    badge.dataset.s = st.state; badge.textContent = st.label;

    renderStep(st);
    renderPeo(slope);
    drawChart();
  }

  // ---------- A：穩定偏差 ----------
  function bareOffset(cup) {
    const d = log.devs.filter((r) => r.cup === cup && r.probe === "bare").map((r) => r.mean - r.glass);
    return d.length ? mean(d) : null;
  }
  function addDev() {
    const glass = num("glass-a");
    if (glass == null) { $("glass-a").setAttribute("aria-invalid", "true"); $("glass-a").focus(); return; }
    $("glass-a").removeAttribute("aria-invalid");
    const t = lastT(), st = stableAt(t);
    if (!st.s.n) { alert("統計視窗內沒有有效讀值"); return; }
    if (!st.ok && !confirm(`目前判定為「${st.label}」，仍要記錄這一點嗎？`)) return;
    const c = ctxNow();
    log.devs.push({ t, cup: c.cup, probe: c.probe, glass, mean: st.s.mean, std: st.s.std, n: st.s.n, stable: st.ok });
    log.events.push({ t, kind: "玻璃讀數", text: `${glass} °C（A 記錄點）` });
    $("glass-a").value = "";
    store.save();
    renderDevs();
  }
  function renderDevs() {
    const tb = $("dev-body"); tb.innerHTML = "";
    log.devs.forEach((r, i) => {
      const off = bareOffset(r.cup);
      const dev = r.probe === "sleeved" ? (off == null ? null : r.mean - (r.glass + off)) : null;
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${timeStr(r.t)}</td><td class="txt">${r.cup}</td><td class="txt">${PROBE_LABEL[r.probe]}</td>`
        + `<td>${fmt(r.glass, 1)}</td><td>${fmt(r.mean, 3)}</td><td>${fmt(r.std, 3)}</td><td class="txt">${r.stable ? "是" : "否"}</td>`
        + `<td>${sgn(r.mean - r.glass, 3)}</td>`
        + `<td>${r.probe === "sleeved" ? (dev == null ? "需先量裸測棒" : sgn(dev, 3)) : "—"}</td>`
        + `<td><button class="del" title="刪除" aria-label="刪除">✕</button></td>`;
      tr.querySelector(".del").onclick = () => { if (confirm("刪除這一點？")) { log.devs.splice(i, 1); store.save(); renderDevs(); } };
      tb.appendChild(tr);
    });
    $("dev-empty").hidden = log.devs.length > 0;
    $("dev-summary").innerHTML = devSummary().map((x) =>
      `${CUP_LABEL[x.cup]}：裸測棒 TX−玻璃 <b>${sgn(x.bare, 3)}</b> °C（${x.nBare} 點）　套膜偏差 <b>${sgn(x.sleeve, 3)}</b> °C（${x.nSleeve} 點）`).join("<br>");
  }
  function devSummary() {
    return ["A", "B"].map((cup) => {
      const bare = bareOffset(cup);
      const sl = log.devs.filter((r) => r.cup === cup && r.probe === "sleeved");
      return { cup, bare, nBare: log.devs.filter((r) => r.cup === cup && r.probe === "bare").length,
        sleeve: bare == null || !sl.length ? null : mean(sl.map((r) => r.mean - (r.glass + bare))), nSleeve: sl.length };
    }).filter((x) => x.nBare || x.nSleeve);
  }

  // ---------- B：反應速度 τ ----------
  function stepStart() {
    const t = lastT(), st = stableAt(t);
    if (!running || !st.s.n) { alert("還沒有讀值，先開始讀取"); return; }
    if (!st.ok && !confirm(`起始杯目前判定為「${st.label}」。τ 需要起始溫度穩定，仍要開始嗎？`)) return;
    const c = ctxNow();
    const cup1 = c.cup === "A" ? "B" : "A";
    const t0 = now();
    log.step = { t0, start: st.s.mean, cup0: c.cup, cup1, probe: c.probe };
    log.events.push({ t: t0, kind: "階躍開始", text: `${c.cup} → ${cup1} 杯，起始 ${fmt(st.s.mean, 2)} °C` });
    $("cup").value = cup1;
    pushCtx({ cup: cup1 });
    render();
  }
  function stepFinish() {
    const s = log.step; if (!s) return;
    let end = num("glass-b"), src = "玻璃溫度計";
    if (end == null) {
      const st = stableAt(lastT());
      if (lastT() - s.t0 < winS()) { alert("階躍後的時間還不到一個統計視窗，等讀值穩定或輸入玻璃讀數"); return; }
      if (!st.ok && !confirm(`新杯目前判定為「${st.label}」，用目前平均 ${fmt(st.s.mean, 2)} °C 當終點嗎？`)) return;
      end = st.s.mean; src = "穩定值";
    }
    if (Math.abs(end - s.start) < 1 && !confirm(`起始與終點只差 ${fmt(Math.abs(end - s.start), 2)} °C，τ 會很不準。仍要計算嗎？`)) return;
    const target = s.start + TAU_FRACTION * (end - s.start);
    const tc = crossTime(s.t0, s.start, target);
    if (tc == null) { alert(`讀值還沒走到 63.2% 目標 ${fmt(target, 2)} °C，請等一下再按`); return; }
    const tr = { t0: s.t0, cup0: s.cup0, cup1: s.cup1, probe: s.probe, start: s.start, end, endSrc: src, target, tau: tc - s.t0, tEnd: lastT() };
    log.trials.push(tr);
    log.events.push({ t: lastT(), kind: "階躍結束", text: `終點 ${fmt(end, 2)} °C（${src}），τ = ${fmt(tr.tau, 1)} 秒` });
    if (src === "玻璃溫度計") log.events.push({ t: lastT(), kind: "玻璃讀數", text: `${end} °C（B 終點）` });
    log.step = null;
    $("glass-b").value = "";
    store.save();
    renderTrials();
    render();
  }
  function stepCancel() {
    if (!log.step || !confirm("取消這次階躍？")) return;
    log.events.push({ t: now(), kind: "階躍取消", text: "" });
    log.step = null; store.save(); render();
  }
  function renderStep(st) {
    const s = log.step;
    $("step-start").disabled = !!s || !running;
    $("step-finish").disabled = !s;
    $("step-cancel").disabled = !s;
    const box = $("step-box");
    if (!s) {
      const c = ctxNow();
      box.innerHTML = `準備：測棒在 <b>${CUP_LABEL[c.cup]}</b>（${PROBE_LABEL[c.probe]}），目前 ${st.ok ? "已穩定" : st.label}，平均 <b>${fmt(st.s.mean, 2)}</b> °C。`
        + `<br>移杯的那一刻按「開始階躍」，起始溫度取按下前 ${winS()} 秒的平均。`;
      return;
    }
    const t = lastT(), cur = readings.length ? val(readings[readings.length - 1]) : null;
    const end = num("glass-b");
    let prog = "";
    if (end != null && cur != null && end !== s.start) {
      const target = s.start + TAU_FRACTION * (end - s.start);
      const tc = crossTime(s.t0, s.start, target);
      prog = `<br>終點 ${fmt(end, 1)} °C → 63.2% 目標 <b>${fmt(target, 2)}</b> °C，目前進度 <b>${fmt((cur - s.start) / (end - s.start) * 100, 0)}%</b>`
        + (tc ? `，已越過目標，τ ≈ <b>${fmt(tc - s.t0, 1)}</b> 秒` : "");
    }
    box.innerHTML = `<b>階躍進行中</b>：${s.cup0} → ${s.cup1} 杯（${PROBE_LABEL[s.probe]}），已經過 <b>${durStr(t - s.t0)}</b>，`
      + `起始 <b>${fmt(s.start, 2)}</b> °C，目前 <b>${fmt(cur, 2)}</b> °C。${prog}`
      + `<br>讀值穩定後按「完成並計算 τ」；有新杯的玻璃讀數就先填上，會用它當終點。`;
  }
  function renderTrials() {
    const tb = $("tau-body"); tb.innerHTML = "";
    log.trials.forEach((tr, i) => {
      const row = document.createElement("tr");
      row.innerHTML = `<td>${i + 1}</td><td class="txt">${tr.end > tr.start ? "升溫" : "降溫"}</td><td class="txt">${PROBE_LABEL[tr.probe]}</td>`
        + `<td>${timeStr(tr.t0)}</td><td>${fmt(tr.start, 2)}</td><td>${fmt(tr.end, 2)}</td><td class="txt">${tr.endSrc}</td>`
        + `<td>${fmt(tr.target, 2)}</td><td>${fmt(tr.tau, 1)}</td><td><button class="del" title="刪除" aria-label="刪除">✕</button></td>`;
      row.querySelector(".del").onclick = () => { if (confirm("刪除這次 τ？")) { log.trials.splice(i, 1); store.save(); renderTrials(); render(); } };
      tb.appendChild(row);
    });
    $("tau-empty").hidden = log.trials.length > 0;
    const m = tauMeans();
    const line = (name, a) => `${name}：${a.length} 次${a.length ? `，τ = ${a.map((x) => x.toFixed(1)).join("、")} 秒，平均 <b>${fmt(mean(a), 1)}</b> 秒，標準差 ${fmt(sd(a), 1)}` : ""}${a.length < 3 ? `（還差 ${3 - a.length} 次）` : ""}`;
    $("tau-summary").innerHTML = log.trials.length
      ? `${line("升溫", m.up)}<br>${line("降溫", m.down)}<br>全部平均 <b>${fmt(m.all, 1)}</b> 秒（C 階段預設使用這個值）` : "";
    $("tau-use").placeholder = m.all == null ? "尚未量測" : `${m.all.toFixed(1)}（B 平均）`;
  }

  // ---------- C：PEO 記錄 ----------
  function peoStart() {
    if (!$("safety-ok").checked) return;
    if (!running) { alert("先開始讀取"); return; }
    const t = now();
    log.peo = { t };
    log.events.push({ t, kind: "PEO 記錄開始", text: "已勾選：已確認隔離／目前為不通電測試" });
    pushCtx({ phase: "C" });
    renderEvents(); render();
  }
  function peoStop() {
    if (!log.peo || !confirm("結束 PEO 記錄？")) return;
    log.events.push({ t: now(), kind: "PEO 記錄結束", text: "" });
    log.peo = null;
    pushCtx({ phase: currentTab() });
    renderEvents(); render();
  }
  function peoEvent(kind) {
    let text = "";
    if (kind === "調電壓") { text = $("volt").value.trim(); if (!text) { $("volt").focus(); return; } $("volt").value = ""; }
    if (kind === "備註") { text = $("note-text").value.trim(); if (!text) { $("note-text").focus(); return; } $("note-text").value = ""; }
    log.events.push({ t: now(), kind, text });
    store.save();
    renderEvents();
  }
  function renderEvents() {
    const tb = $("event-body"); tb.innerHTML = "";
    const evs = log.events.slice().reverse();
    for (const e of evs) {
      const i = upper(e.t) - 1;
      const v = i >= 0 ? val(readings[i]) : null;
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${timeStr(e.t)}</td><td>${log.peo || e.t >= (peoStartOf(e.t) ?? Infinity) ? durStr(e.t - (peoStartOf(e.t) ?? e.t)) : "—"}</td>`
        + `<td class="txt">${esc(e.kind)}${e.text ? "：" + esc(e.text) : ""}</td><td>${v == null ? "--" : fmt(v, 1)}</td>`;
      tb.appendChild(tr);
    }
  }
  // 時間 t 所在（或最近一次開始）的 PEO 記錄開始時間
  function peoStartOf(t) {
    let s = null;
    for (const e of log.events) { if (e.t > t) break; if (e.kind === "PEO 記錄開始") s = e.t; }
    return s;
  }
  function renderPeo(slope) {
    const ok = $("safety-ok").checked;
    $("peo-start").disabled = !ok || !!log.peo || !running;
    $("peo-stop").disabled = !log.peo;
    for (const b of document.querySelectorAll("[data-event]")) b.disabled = !log.peo;
    $("volt").disabled = $("note-text").disabled = !log.peo;
    const tau = tauUsed();
    const last = readings[readings.length - 1];
    const v = last ? val(last) : null;
    $("c-tau").textContent = tau == null ? "--" : `${fmt(tau, 1)} 秒`;
    $("c-slope").textContent = slope == null ? "--" : `${sgn(slope * 60, 2)} °C/分`;
    $("c-lag").textContent = slope == null || tau == null ? "--" : `${sgn(slope * tau, 2)} °C`;
    const corrOn = $("corr-on").value === "1";
    $("c-corr").textContent = corrOn && v != null && slope != null && tau != null ? `${fmt(v + tau * slope, 2)} °C` : "--";
    $("lg-corr").hidden = !(corrOn && tau != null);
  }

  // ---------- 分頁 ----------
  function currentTab() { return document.querySelector(".tab[aria-selected='true']").dataset.tab; }
  function selectTab(tab, record = true) {
    for (const b of document.querySelectorAll(".tab")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    for (const p of document.querySelectorAll(".panel")) p.hidden = p.dataset.panel !== tab;
    if (record && log && !log.peo && ctxNow().phase !== tab) pushCtx({ phase: tab });
    if (log) { log.settings.tab = tab; store.save(); }
    render();
  }

  // ---------- 圖表 ----------
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function drawChart() {
    const cv = $("chart"), dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = cssVar("--chart-bg"); ctx.fillRect(0, 0, w, h);
    const L = 56, R = 12, T = 14, B = 26, pw = w - L - R, ph = h - T - B;
    ctx.font = `11px ${cssVar("--mono")}`;
    ctx.fillStyle = cssVar("--chart-tick");
    if (!readings.length) { ctx.fillText("尚無資料", L + 8, T + 18); return; }

    const end = lastT();
    const spanSel = +$("chart-span").value;
    const span = Math.max(spanSel || end - readings[0][0], 10);
    const t0 = end - span;
    const vis = readings.slice(Math.max(0, upper(t0) - 1));
    const tau = tauUsed(), corrOn = $("corr-on").value === "1" && tau != null;
    const sw = +$("slope-win").value;
    const corr = corrOn ? vis.map((r) => { const v = val(r); const s = v == null ? null : slopeAt(r[0], sw); return s == null ? null : v + tau * s; }) : [];

    // 參考線：B 進行中顯示 63.2% 目標，否則顯示 A 的玻璃讀數
    let ref = null;
    if (log.step && num("glass-b") != null) ref = log.step.start + TAU_FRACTION * (num("glass-b") - log.step.start);
    else if (currentTab() === "A") ref = num("glass-a");

    const vals = vis.map(val).filter((v) => v != null).concat(corr.filter((v) => v != null));
    if (ref != null) vals.push(ref);
    let lo = vals.length ? Math.min(...vals) : 0, hi = vals.length ? Math.max(...vals) : 1;
    const padY = Math.max((hi - lo) * 0.1, 0.05); lo -= padY; hi += padY;
    const x = (t) => L + ((t - t0) / span) * pw;
    const y = (v) => T + (1 - (v - lo) / (hi - lo)) * ph;

    const step = niceStep((hi - lo) / 5), dec = Math.max(0, -Math.floor(Math.log10(step)));
    ctx.strokeStyle = cssVar("--chart-grid"); ctx.lineWidth = 1;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      ctx.beginPath(); ctx.moveTo(L, Math.round(y(v)) + .5); ctx.lineTo(L + pw, Math.round(y(v)) + .5); ctx.stroke();
      ctx.fillText(v.toFixed(dec), L - 6, y(v));
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    const tStep = niceTimeStep(span / 6);
    for (let t = Math.ceil(t0 / tStep) * tStep; t <= end; t += tStep) ctx.fillText(timeStr(t), x(t), T + ph + 6);

    // 統計視窗
    ctx.fillStyle = cssVar("--accent"); ctx.globalAlpha = 0.10;
    const wx = Math.max(x(end - winS()), L);
    ctx.fillRect(wx, T, L + pw - wx, ph);
    ctx.globalAlpha = 1;

    // 事件
    ctx.save();
    ctx.beginPath(); ctx.rect(L, 0, pw, h); ctx.clip();
    ctx.strokeStyle = cssVar("--text-3"); ctx.fillStyle = cssVar("--text-2"); ctx.setLineDash([2, 3]);
    ctx.textAlign = "left"; ctx.textBaseline = "top";
    const marks = log.events.map((e) => [e.t, e.kind]);
    if (log.step) marks.push([log.step.t0, "階躍 t0"]);
    for (const [t, k] of marks) {
      if (t < t0 || t > end) continue;
      ctx.beginPath(); ctx.moveTo(x(t) + .5, T); ctx.lineTo(x(t) + .5, T + ph); ctx.stroke();
      ctx.fillText(k, x(t) + 3, 1);
    }
    ctx.setLineDash([]);
    ctx.restore();

    if (ref != null) {
      ctx.strokeStyle = cssVar("--ok"); ctx.setLineDash([5, 4]);
      ctx.beginPath(); ctx.moveTo(L, y(ref)); ctx.lineTo(L + pw, y(ref)); ctx.stroke(); ctx.setLineDash([]);
    }
    const line = (series, color, dash) => {
      ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.setLineDash(dash); ctx.beginPath();
      let pen = false;
      vis.forEach((r, i) => {
        const v = series(r, i);
        if (v == null || r[0] < t0) { pen = false; return; }
        if (pen) ctx.lineTo(x(r[0]), y(v)); else ctx.moveTo(x(r[0]), y(v));
        pen = true;
      });
      ctx.stroke(); ctx.setLineDash([]);
    };
    if (corrOn) line((r, i) => corr[i], cssVar("--warn"), [6, 4]);
    line((r) => val(r), cssVar("--chart-wave"), []);
    ctx.fillStyle = cssVar("--err");
    for (const r of vis) if (r[0] >= t0 && val(r) == null) ctx.fillRect(x(r[0]) - 1, T + ph - 6, 3, 6);
    ctx.strokeStyle = cssVar("--chart-axis"); ctx.strokeRect(L + .5, T + .5, pw, ph);
  }
  function niceStep(raw) {
    const p = 10 ** Math.floor(Math.log10(raw || 1)), m = raw / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
  }
  function niceTimeStep(raw) {
    for (const s of [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]) if (s >= raw) return s;
    return 14400;
  }

  // ---------- 匯出 ----------
  function csvCell(v) {
    if (v == null) return "";
    const s = String(v);
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }
  function download(name, rows) {
    const csv = "﻿" + rows.map((r) => r.map(csvCell).join(",")).join("\r\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  const stamp = () => { const d = new Date();
    return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`; };

  function exportRaw() {
    if (!readings.length) { $("msg").textContent = "還沒有讀值"; return; }
    // 事件和玻璃讀數掛在發生時間之後的第一筆讀值上（最後一筆之後的掛在最後一筆）
    const evAt = new Map(), glassAt = new Map();
    for (const e of log.events) {
      const i = Math.min(upper(e.t - 1e-6), readings.length - 1);
      evAt.set(i, (evAt.has(i) ? evAt.get(i) + " | " : "") + e.kind + (e.text ? "：" + e.text : ""));
    }
    for (const r of log.devs) glassAt.set(Math.min(upper(r.t - 1e-6), readings.length - 1), r.glass);
    for (const tr of log.trials) if (tr.endSrc === "玻璃溫度計") glassAt.set(Math.min(upper(tr.tEnd - 1e-6), readings.length - 1), tr.end);
    const tau = tauUsed(), corrOn = $("corr-on").value === "1" && tau != null, sw = +$("slope-win").value;
    const rows = [["Timestamp", "Elapsed(s)", "Channel", "Reading(C)", "Corrected(C)", "dTdt(C/min)", "T1(C)", "T2(C)",
      "Phase", "Cup", "Probe", "Event", "Glass(C)", "Stable", "Flags", "RawT1", "RawT2", "Status"]];
    readings.forEach((r, i) => {
      const v = val(r), c = ctxAt(r[0]);
      const slope = v == null ? null : slopeAt(r[0], sw);
      const flags = [];
      if (v == null) flags.push(errOf(r));
      if (isSpike(i)) flags.push(`跳動 ${sgn(v - val(readings[prevValid(i)]), 1)}°C`);
      rows.push([isoStr(r[0]), (r[0] - log.session).toFixed(3), `T${ch()}`, v, corrOn && slope != null ? (v + tau * slope).toFixed(3) : null,
        slope == null ? null : (slope * 60).toFixed(4), r[1], r[2], PHASE_LABEL[c.phase], CUP_LABEL[c.cup], PROBE_LABEL[c.probe],
        evAt.get(i) ?? null, glassAt.get(i) ?? null, v == null ? null : (stableAt(r[0]).ok ? 1 : 0), flags.join("；") || null,
        r[5] ? r[5][0] : null, r[5] ? r[5][1] : null, r[5] ? "0x" + r[5][2].toString(16).padStart(4, "0") : null]);
    });
    download(`tx600n_record_${stamp()}.csv`, rows);
  }

  function exportSummary() {
    const m = tauMeans(), tau = tauUsed();
    const rows = [["TX-600N 校正摘要"], ["匯出時間", isoStr(now())], ["讀取開始", isoStr(log.session)], ["分析通道", `T${ch()}`],
      ["穩定判定", `${winS()} 秒內變化 ≤ ${thC()} °C；套膜至少浸泡 ${soakS() / 60} 分鐘`], [],
      ["A 穩定偏差"], ["Timestamp", "Cup", "Probe", "Glass(C)", "TXMean(C)", "TXStd(C)", "N", "Stable", "TX-Glass(C)", "SleeveDeviation(C)"]];
    for (const r of log.devs) {
      const off = bareOffset(r.cup);
      rows.push([isoStr(r.t), CUP_LABEL[r.cup], PROBE_LABEL[r.probe], r.glass, r.mean.toFixed(4), r.std.toFixed(4), r.n, r.stable ? 1 : 0,
        (r.mean - r.glass).toFixed(4), r.probe === "sleeved" && off != null ? (r.mean - (r.glass + off)).toFixed(4) : null]);
    }
    rows.push([], ["Cup", "BareOffset TX-Glass(C)", "N bare", "SleeveDeviation mean(C)", "N sleeved"]);
    for (const x of devSummary()) rows.push([CUP_LABEL[x.cup], x.bare == null ? null : x.bare.toFixed(4), x.nBare, x.sleeve == null ? null : x.sleeve.toFixed(4), x.nSleeve]);
    rows.push([], ["B 反應速度 τ（63.2% 響應時間）"], ["#", "Direction", "Probe", "t0", "From", "To", "Start(C)", "End(C)", "EndSource", "Target(C)", "Tau(s)"]);
    log.trials.forEach((tr, i) => rows.push([i + 1, tr.end > tr.start ? "升溫" : "降溫", PROBE_LABEL[tr.probe], isoStr(tr.t0), tr.cup0, tr.cup1,
      tr.start.toFixed(3), tr.end.toFixed(3), tr.endSrc, tr.target.toFixed(3), tr.tau.toFixed(2)]));
    rows.push([], ["Direction", "N", "TauMean(s)", "TauStd(s)"]);
    for (const [k, a] of [["升溫", m.up], ["降溫", m.down], ["全部", [...m.up, ...m.down]]]) rows.push([k, a.length, a.length ? mean(a).toFixed(2) : null, a.length > 1 ? sd(a).toFixed(2) : null]);
    rows.push([], ["C 使用的 τ(s)", tau == null ? null : tau.toFixed(2)], ["dT/dt 平滑視窗(s)", $("slope-win").value], [], ["事件"], ["Timestamp", "Event", "Text"]);
    for (const e of log.events) rows.push([isoStr(e.t), e.kind, e.text]);
    download(`tx600n_summary_${stamp()}.csv`, rows);
  }

  // ---------- 設定還原 ----------
  function applyLogToInputs() {
    const c = ctxNow();
    $("cup").value = c.cup; $("probe").value = c.probe;
    const st = log.settings;
    for (const id of ["channel", "win", "th", "soak", "tau-use", "slope-win", "corr-on"]) if (st[id] != null) $(id).value = st[id];
    selectTab(log.peo ? "C" : (st.tab || c.phase), false);
    renderDevs(); renderTrials(); renderEvents();
  }

  // ---------- 事件綁定 ----------
  $("btn-start").onclick = start;
  $("btn-stop").onclick = stop;
  $("refresh-com").onclick = loadCom;
  $("cup").onchange = () => { pushCtx({ cup: $("cup").value }); render(); };
  $("probe").onchange = () => { pushCtx({ probe: $("probe").value }); render(); };
  $("add-dev").onclick = addDev;
  $("glass-a").addEventListener("keydown", (e) => { if (e.key === "Enter") addDev(); });
  $("step-start").onclick = stepStart;
  $("step-finish").onclick = stepFinish;
  $("step-cancel").onclick = stepCancel;
  $("peo-start").onclick = peoStart;
  $("peo-stop").onclick = peoStop;
  $("safety-ok").onchange = render;
  for (const b of document.querySelectorAll("[data-event]")) b.onclick = () => peoEvent(b.dataset.event);
  for (const b of document.querySelectorAll(".tab")) b.onclick = () => selectTab(b.dataset.tab);
  for (const id of ["channel", "win", "th", "soak", "tau-use", "slope-win", "corr-on"]) {
    $(id).addEventListener("input", () => {
      if (log) { log.settings[id] = $(id).value; store.save(); }
      if (id === "channel") { renderEvents(); lastSpikeIdx = -1; alarm = null; }
      render();
    });
  }
  for (const id of ["glass-a", "glass-b", "chart-span"]) $(id).addEventListener("input", render);
  $("export-raw").onclick = exportRaw;
  $("export-summary").onclick = exportSummary;
  window.addEventListener("resize", render);
  window.addEventListener("beforeunload", (e) => { if (log && log.peo) { e.preventDefault(); e.returnValue = ""; } });

  log = newLog(now());
  loadCom();
  poll();
})();
