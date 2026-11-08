/* 介面語言（中文／English）：監測頁與實驗紀錄頁共用。
 *
 * 用法
 *   ?lang=en 切到英文，?lang=zh 切回中文；選擇存在 localStorage（存不了就只在這一頁有效）。
 *   沒有 ?lang 也沒有記住的選擇 → 中文，畫面與原本完全相同（本檔不改任何中文）。
 *
 * 做法（無外部相依、無建置步驟）
 *   字典以「中文原文」為 key、英文為值；中文模式什麼都不做。
 *   英文模式：載入時與之後每次 DOM 變動，把文字節點與 title／placeholder／aria-label 依字典換成英文。
 *   - EN：精確比對（整段文字，前後空白保留）。
 *   - PATTERNS：帶數字或變數的句子（正規式；$1… 會先翻譯再代入），伺服器回傳的訊息（感測器狀態、通知、
 *     錯誤）也走這裡，所以 API 回應格式不變，翻譯全在前端。
 *   - 找不到就依「\n」「；」「 · 」「、」「，」「：」拆開逐段翻；仍找不到的維持原文（不會出錯）。
 *   canvas 內的文字不是 DOM，monitor.js／history.js 在繪製處呼叫 I18N.tr(文字)。
 *   新增中文字串時：在 EN 或 PATTERNS 補一筆即可；沒補只會在英文模式顯示中文。
 *   某個節點不要翻譯：加 data-no-i18n 屬性（含子孫）。
 */
(() => {
  "use strict";
  const KEY = "ui_lang";
  const CJK = /[\u3000-\u303f\u3400-\u9fff\uff00-\uffef]/;

  function pickLang() {
    let q = null;
    try { q = new URLSearchParams(location.search).get("lang"); } catch { /* 舊瀏覽器 */ }
    if (q) {
      const l = /^en/i.test(q) ? "en" : "zh";
      try { localStorage.setItem(KEY, l); } catch { /* 存不了就只在這次有效 */ }
      return l;
    }
    try { if (localStorage.getItem(KEY) === "en") return "en"; } catch { /* ignore */ }
    return "zh";
  }
  const lang = pickLang();

  // ---------------------------------------------------------------------------------------------
  // 精確比對
  // ---------------------------------------------------------------------------------------------
  const EN = {
    // ---- 頁首 / 通用 ----
    "感測器整合系統": "Sensor Monitor",
    "這些感測器的數值是模擬產生的，不是真實量測": "These sensor values are simulated, not real measurements",
    "模擬模式": "Simulation mode",
    "瀏覽過去的實驗並把數據畫成圖": "Browse past experiments and plot their data",
    "實驗紀錄": "Experiment history",
    "實驗參數": "Experiment setup",
    "填寫實驗標籤與電源參數、檢驗後開始實驗": "Fill in the experiment labels and power parameters, run the check, then start",
    "電源 COM 埠、感測器裝置與參數、Sensor 測試模式、電供設定手動模式": "Power supply COM port, sensor devices and parameters, sensor test mode, manual power-supply setup mode",
    "⚙ 設定": "⚙ Settings",
    "與伺服器的即時連線": "Live connection to the server",
    "連線中": "Connecting",
    "已連線": "Connected",
    "重新連線中": "Reconnecting",
    "緊急停止": "Emergency stop",
    "狀態顯示": "Status",
    "待機中": "Standby", "狀態": "Status",
    "關閉": "Close",
    "取消": "Cancel",
    "確定": "OK",
    "警告": "Warning",
    "錯誤": "Error",
    "繼續監測": "Continue monitoring",
    "即時監測": "Live monitor",
    "回到即時監測": "Back to the live monitor",

    // ---- 感測器卡 ----
    "溫度": "Temperature", "音訊": "Audio", "距離": "Distance", "光譜儀": "Spectrometer", "光譜": "Spectrum",
    "系統": "System", "電源": "Power supply", "模擬": "Sim", "模擬音訊": "Simulated audio",
    "待機": "Standby", "未接入": "Not integrated",
    "初始化中": "Initializing", "正常": "OK", "已停止": "Stopped",
    "驅動／韌體問題": "Driver / firmware problem", "找不到裝置": "Device not found",
    "Port 衝突／被佔用": "Port conflict / in use", "數據收不進來": "No data received",
    "接線／訊號異常": "Wiring / signal fault", "超出量程": "Out of range",
    "未選擇／未啟用": "Not selected / disabled", "其他錯誤": "Other error",
    "未啟用": "Disabled", "等待資料": "Waiting for data", "尚未開始監測": "Monitoring not started",
    "讀取失敗": "Read failed", "通訊中斷": "Communication lost", "沒有有效值": "No valid value",
    "感測器斷線": "Sensor disconnected", "連接錯誤": "Connection error", "錄音中": "Recording",
    "量測中": "Measuring", "初始化失敗": "Initialization failed", "錄製錯誤": "Recording error",
    "全為 0": "All zeros", "訊號全為 0": "Signal all zeros", "量測值無效": "Invalid reading", "讀值錯誤": "Read error",
    "等待超時": "Wait timed out", "無可用設備": "No device available", "沒有找到光譜儀": "No spectrometer found",
    "無感測器": "No sensors",
    "每秒更新": "updates every second",
    "絕對距離": "Absolute distance",
    "0-漫反射": "0 - Diffuse", "1-鏡面反射": "1 - Specular",
    "無可用音訊設備": "No audio device available", "音訊設備檢測失敗": "Audio device detection failed",
    "未偵測到序列埠，可直接輸入": "No serial port detected; you can type one in",
    "無法取得序列埠清單": "Could not get the serial port list",
    "可從清單選擇或直接輸入": "Pick from the list or type it in",
    "實驗錄製中": "Experiment recording",
    "實驗錄製中才記錄": "Run only",
    "飽和：已達 65535 counts 上限": "Saturated: reached the 65535 counts limit",

    // ---- 電源面板 ----
    "電源 TruPlasma": "Power supply TruPlasma",
    "電供設定手動模式：這個模式不會錄製資料": "Manual power-supply setup mode: no data is recorded in this mode",
    "電供設定手動模式": "Manual power-supply setup mode",
    "離開電供設定手動模式": "Exit manual power-supply setup mode",
    "進入電供設定手動模式": "Enter manual power-supply setup mode",
    "未連線": "Not connected", "連線": "Connect", "斷線": "Disconnect",
    "只有接電源的電腦本機可以控制": "Only the computer connected to the power supply can control it",
    "重設警報": "Reset alarms",
    "正向 (A)": "Forward (A)", "負向 (B)": "Reverse (B)",
    "平均電壓（V）": "Avg. voltage (V)", "平均電流（A）": "Avg. current (A)",
    "設定": "Settings",
    "模式": "Mode", "單極": "Unipolar", "雙極": "Bipolar", "輸出": "Output", "單極 A": "Unipolar A", "單極 B": "Unipolar B",
    "頻率 f（Hz）": "Frequency f (Hz)", "頻率 f": "Frequency f",
    "佔空比（%）": "Duty cycle (%)", "正向佔空比（%）": "Forward duty cycle (%)", "負向佔空比（%）": "Reverse duty cycle (%)",
    "佔空比": "Duty cycle", "正向佔空比": "Forward duty cycle", "負向佔空比": "Reverse duty cycle",
    "電壓上限（V）": "Voltage limit (V)", "電流上限（A）": "Current limit (A)",
    "電壓上限": "Voltage limit", "電流上限": "Current limit",
    "正負向共用（此電源只有一組上限）": "Shared by both polarities (this supply has only one set of limits)",
    "未套用": "Not applied", "套用設定": "Apply settings", "套用": "Apply",
    "輸出中，設定已鎖定": "Output on, settings locked",
    "開始輸出": "Start output", "停止輸出": "Stop output", "緊急關閉": "Emergency off",
    "進階": "Advanced", "功率上限（kW）": "Power limit (kW)", "功率上限": "Power limit", "COM 埠": "COM port",
    "RS232 控制": "RS232 control", "主繼電器": "Main relay",
    "電壓": "Voltage", "電流": "Current",
    "A（單極為脈衝電流，雙極為 RMS）": "A (pulse current for unipolar, RMS for bipolar)",
    "重設電弧計數": "Reset arc counters",
    "電源參數": "Power supply parameters",
    "參數會寫入 EEPROM，只能在輸出關閉時修改；寫入後會讀回比對。": "Parameters are written to the EEPROM and can only be changed while output is off; they are read back and compared after writing.",
    "讀取參數": "Read parameters", "讀取中…": "Reading…", "寫入": "Write",
    "尚未讀取：連線後按「讀取參數」。": "Not read yet: connect, then press \"Read parameters\".",
    "脈衝與頻率": "Pulse and frequency", "電弧管理": "Arc management", "點火": "Ignition", "通訊（唯讀）": "Communication (read-only)",
    "未知": "Unknown", "警報": "Alarm", "0（關）": "0 (off)", "1（開）": "1 (on)",
    "電源異常": "Power supply fault",
    "電源訊息": "Power supply message",
    "只有在接電源的電腦本機上才能控制電源": "The power supply can only be controlled from the computer connected to it",
    "電源未連線": "Power supply not connected",
    "電源沒有回應（逾時）": "Power supply did not respond (timeout)",

    // ---- 圖表 ----
    "圖表": "Charts",
    "Temperature（溫度）": "Temperature",
    "Distance（距離）": "Distance",
    "PSU Voltage（電壓（V））": "PSU Voltage (V)",
    "PSU Current（電流（A））": "PSU Current (A)",
    "Spectrogram（聲音頻譜圖）": "Spectrogram (audio)",
    "Optical Spectrum（光譜）": "Optical Spectrum",
    "正向平均": "Fwd avg.", "負向平均（虛線）": "Rev avg. (dashed)",
    "輸出電壓（電源控制值）": "Output",
    "電壓（V）正向 / 負向平均": "Voltage (V), forward / reverse avg.",
    "電流（A）正向 / 負向平均": "Current (A), forward / reverse avg.",
    
    // ---- 本次參數面板 ----
    "本次參數": "Run parameters",
    "收合／展開本次參數": "Collapse / expand run parameters",
    "總長": "Total", "剩餘": "Remaining", "實驗標籤": "Experiment labels",
    "結束實驗": "End experiment", "結束這場": "End this run", "停止錄製": "Stop recording",
    "儲存中…": "Saving…", "結束中…": "Ending…", "準備中…": "Preparing…",
    "準備中": "Preparing", "錄製基線": "Recording baseline", "開啟輸出中": "Starting output",
    "輸出中": "Output on", "結束中": "Ending", "已緊急停止": "Emergency stopped", "已結束": "Finished",
    "錄製中": "Recording", "Sensor 測試": "Sensor test",
    "金屬": "Metal", "試片": "Specimen", "電解液": "Electrolyte", "添加物": "Additive",
    "控制方式": "Control mode", "定電壓": "Constant voltage", "定電流": "Constant current",
    "只錄感測器，不控制電源（非正式實驗）": "Records sensors only, does not control the power supply (not a formal experiment)",
    "Sensor 測試模式 · 非正式實驗": "Sensor test mode · not a formal experiment",
    "只錄感測器，不連線、不控制電源；存檔標記為測試模式（mode = sensor_test）": "Records sensors only; no connection to or control of the power supply; saved data is marked as test mode (mode = sensor_test)",
    "準備開始：寫入並讀回電源參數、開始錄製": "Preparing: writing and reading back the power parameters, starting the recording",
    "開啟輸出中（主繼電器 → 等待就緒）": "Starting output (main relay → waiting for ready)",
    "結束中：關閉輸出 → 停止錄製": "Ending: turning output off → stopping the recording",
    "已緊急停止 · 感測器繼續錄製": "Emergency stopped · sensors keep recording",

    // ---- 實驗參數視窗 ----
    "金屬種類": "Metal type", "例：AZ31 鎂合金": "e.g. AZ31 magnesium alloy",
    "試片形狀": "Specimen shape", "正方形": "Square", "圓形": "Round",
    "長": "Length", "寬": "Width", "厚": "Thickness", "面積": "Area", "厚度": "Thickness",
    "長（cm）": "Length (cm)", "寬（cm）": "Width (cm)", "厚（cm）": "Thickness (cm)",
    "面積（cm²）": "Area (cm²)", "厚度（cm）": "Thickness (cm)",
    "長 × 寬 × 厚，單位 cm": "Length × width × thickness, in cm",
    "面積 cm²、厚度 cm": "Area in cm², thickness in cm",
    "電解液配方與濃度": "Electrolyte composition and concentration",
    "例：Na2SiO3 10 g/L + KOH 2 g/L": "e.g. Na2SiO3 10 g/L + KOH 2 g/L",
    "添加物（選填）": "Additive (optional)", "例：TiO2 奈米粒子 5 g/L": "e.g. TiO2 nanoparticles 5 g/L",
    "先填完實驗標籤": "Fill in the experiment labels first",
    "電壓設定（V）": "Voltage setpoint (V)", "電流設定（A）": "Current setpoint (A)",
    "電壓設定": "Voltage setpoint", "電流設定": "Current setpoint",
    "實驗時間": "Experiment duration", "實驗時間（分、秒）": "Experiment duration (min, sec)",
    "實驗時間（分）": "Experiment duration (min)", "實驗時間（秒）": "Experiment duration (sec)",
    "分": "min", "秒": "s",
    "按「檢驗Sensor與電供」就會把以上參數": "Pressing \"Check sensors & PSU\" writes the parameters above ",
    "寫入電源": "to the power supply",
    "（EEPROM）並讀回比對；開始前再確認一次。檢驗結果 5 分鐘內有效；實驗中鎖定，不能修改": " (EEPROM) and reads them back to verify; confirm once more before starting. The check result is valid for 5 minutes; settings are locked during the experiment and cannot be changed",
    "尚未檢驗：填完 ①② 後按「檢驗Sensor與電供」": "Not checked yet: fill in ① and ②, then press \"Check sensors & PSU\"",
    "試收四個感測器 3 秒，並把電源參數寫入電源、讀回比對": "Collect 3 s of data from the four sensors, write the power parameters to the supply and read them back",
    "檢驗Sensor與電供": "Check sensors & PSU", "檢驗中…": "Checking…",
    "開始實驗": "Start experiment",
    "不能開始：電源不正常": "Cannot start: power supply not OK",
    "電源正常；部分 sensor 異常（可選「仍要開始」）": "Power supply OK; some sensors have problems (you can choose \"Start anyway\")",
    "實驗時間必填（分、秒），秒須 0–59，總長大於 0": "Experiment duration is required (min, sec); seconds must be 0–59 and the total greater than 0",
    "電源讀回": "Power supply readback",
    "仍要開始": "Start anyway",
    "正常（參數已寫入電源並讀回一致）": "OK (parameters written to the power supply and read back identically)",

    // ---- 設定抽屜 ----
    "電源 COM 埠": "Power supply COM port",
    "裝置": "Devices", "溫度感測器 COM 端口": "Temperature sensor COM port",
    "例：COM3 或 /dev/cu.usbserial": "e.g. COM3 or /dev/cu.usbserial",
    "重新整理 COM": "Refresh COM ports", "音訊設備": "Audio device", "重新整理音訊": "Refresh audio devices",
    "音訊參數": "Audio parameters", "採樣率": "Sample rate", "更新間隔": "Update interval",
    "頻譜圖顯示長度": "Spectrogram display length",
    "只影響畫面，不影響存檔；存檔一律保存全程": "Affects the display only, not saved files; saved files always keep the whole recording",
    "測距儀": "Rangefinder", "反射模式": "Reflection mode", "反射模式說明": "About reflection mode",
    "：霧面、粗糙表面，如塗層、陶瓷、未拋光金屬、紙、樹脂；感測頭垂直對準待測面。": ": matte or rough surfaces such as coatings, ceramics, unpolished metal, paper, resin; point the sensor head straight at the surface.",
    "：鏡面、玻璃、拋光金屬；感測頭需傾斜安裝，讓反射光回到受光部（角度依 KEYENCE 說明書）。": ": mirror, glass, polished metal; mount the sensor head tilted so the reflected light returns to the receiver (angle per the KEYENCE manual).",
    "讀值不穩或讀不到時，先確認模式是否符合表面。": "If readings are unstable or missing, first check that the mode matches the surface.",
    "測距間隔": "Measurement interval",
    "程式讀取測距儀的頻率，0.1 秒＝每秒 10 筆，決定存檔資料密度": "How often the program reads the rangefinder; 0.1 s = 10 readings per second; sets the density of the saved data",
    "量測間隔": "Measurement interval", "光譜儀參數說明": "About spectrometer parameters",
    "：每隔多久量一筆光譜。": ": how often a spectrum is taken.",
    "積分時間": "Integration time",
    "：越長訊號越強，但峰值超過 65535 counts 會飽和。": ": longer means a stronger signal, but peaks above 65535 counts saturate.",
    "Sensor 測試模式（不控制電源）": "Sensor test mode (no power-supply control)",
    "只錄感測器、完全不碰電源，用來排查 sensor 系統錯誤": "Records sensors only and never touches the power supply; used to troubleshoot the sensor system",
    "直接操作電源面板（不會錄製資料）": "Operate the power-supply panel directly (no data is recorded)",
    "Sensor 測試模式": "Sensor test mode",

    // ---- 對話框 ----
    "部分 sensor 異常": "Some sensors have problems",
    "檢驗Sensor與電供 發現以下問題，這場將沒有這些資料（存檔與摘要會標記缺少）：": "\"Check sensors & PSU\" found the following problems; this run will have no data from them (saved files and the summary will mark them as missing):",
    "建議先排除問題再開始。仍要開始？": "We recommend fixing the problems first. Start anyway?",
    "確認輸出高壓": "Confirm high-voltage output",
    "即將開始正式實驗並輸出高壓（最高 電壓 1000 V / 電流 25 A / 功率 10 kW）。請確認電解槽與安全防護都已就緒。": "About to start the formal experiment and output high voltage (max 1000 V / 25 A / 10 kW). Make sure the electrolytic cell and safety guards are ready.",
    "確定開始？": "Start now?",
    "只錄感測器，用來排查 sensor 系統錯誤。": "Records sensors only; used to troubleshoot the sensor system.",
    "完全不碰電源：不連線、不送任何命令": "Never touches the power supply: no connection, no commands sent",
    "存檔標記為測試模式（非正式實驗）；實驗標籤可不填": "Saved data is marked as test mode (not a formal experiment); experiment labels may be left empty",
    "開始測試錄製？": "Start test recording?", "開始測試": "Start test",
    "預檢失敗：伺服器沒有回應": "Pre-check failed: the server did not respond",
    "檢測到以下問題:": "The following problems were detected:", "是否繼續測試？": "Continue the test?", "繼續測試": "Continue test",
    "電源會先關閉輸出與主繼電器，接著停止錄製並存檔。": "The power supply will first turn off its output and main relay, then the recording is stopped and saved.",
    "確定結束實驗？": "End the experiment?", "繼續實驗": "Keep running",
    "停止失敗": "Failed to stop",
    "電源仍在輸出": "Power supply still outputting",
    "錄製已停止，但電源高壓輸出還開著。": "Recording has stopped, but the high-voltage output is still on.",
    "要現在關閉輸出嗎？": "Turn the output off now?",
    "請到接電源的電腦關閉輸出，或按緊急停止。": "Turn the output off on the computer connected to the power supply, or press Emergency stop.",
    "關閉輸出": "Turn output off", "知道了": "Got it", "保持輸出": "Keep output on",
    "緊急停止送出失敗：請改用電源本體的急停或主開關": "Emergency stop could not be sent: use the power supply's own emergency stop or main switch",
    "直接操作電源面板（接電阻測試、維修用）。": "Operate the power-supply panel directly (for resistor tests and maintenance).",
    "這個模式不會錄製資料": "No data is recorded in this mode",
    "進入電供設定手動模式？": "Enter manual power-supply setup mode?",
"變更將寫入電源。": "The changes will be written to the power supply.",
    "確定套用？": "Apply?",
    "輸入框的設定值還沒套用：請先按「套用設定值」再開啟輸出": "The values in the input boxes have not been applied: press \"Apply settings\" before turning the output on",
    "⚠ 目前沒有在錄製實驗：輸出期間的電壓電流不會存檔": "⚠ No experiment is being recorded: voltage and current during output will not be saved",
    "開啟輸出": "Turn output on",
    "即將開啟高壓輸出（最高 電壓 1000 V / 電流 25 A / 功率 10 kW）。請確認電解槽與安全防護都已就緒。": "About to turn on the high-voltage output (max 1000 V / 25 A / 10 kW). Make sure the electrolytic cell and safety guards are ready.",
    "確定要開啟輸出？": "Turn the output on?",
    "沒有錄製也要開啟輸出？（要錄製請先取消，開始監測）": "Turn the output on without recording? (To record, cancel and start monitoring first.)",
    "仍然開啟輸出": "Turn output on anyway",
    "寫入電源參數": "Write power parameter",
    "確定要寫入？": "Write it?",
    "電源操作失敗": "Power supply operation failed", "寫入失敗": "Write failed", "讀取參數失敗": "Failed to read parameters",

    // ---- 伺服器：實驗狀態 / 結果 ----
    "正式實驗": "Formal experiment",
    "Sensor 測試模式（非正式實驗，不控制電源）": "Sensor test mode (not a formal experiment, power supply not controlled)",
    "Sensor 測試模式（非正式實驗）": "Sensor test mode (not a formal experiment)",
    "手動停止": "Stopped manually", "關閉終端機視窗": "Terminal window closed", "關閉主控台視窗": "Console window closed",
    "計時結束": "Timer finished", "完成": "Completed",
    "中止（使用者）": "Aborted (user)", "中止（電源警報）": "Aborted (power alarm)", "中止（Interlock）": "Aborted (interlock)",
    "中止（通訊中斷）": "Aborted (communication lost)", "中止（電源無法開啟輸出）": "Aborted (power supply could not turn output on)",
    "中止（電源自行關閉輸出）": "Aborted (power supply turned output off by itself)",
    "中止（程式關閉或錄製被外部停止）": "Aborted (program closed or recording stopped externally)",
    "正式實驗進行中，電源參數與輸出已由實驗流程鎖定；要停止請按「緊急停止」或「結束實驗」": "Formal experiment in progress: power parameters and output are locked by the experiment flow; press \"Emergency stop\" or \"End experiment\" to stop",
    "雙極脈衝": "Bipolar pulse", "自動": "Auto", "自動偵測": "Auto-detect",
    "監測已在進行中": "Monitoring is already running",
    "正在檢驗Sensor與電供": "Checking sensors & PSU",
    "檢驗Sensor與電供 中…": "Checking sensors & PSU…",
    "至少需要啟用一個感測器才能開始監測": "At least one sensor must be enabled to start monitoring",
    "停止中（儲存資料）": "Stopping (saving data)", "監測已停止": "Monitoring stopped",
    "實驗完成，資料已存檔": "Experiment finished, data saved",
    "實驗中止": "Experiment aborted",
    "電源輸出已關閉；感測器繼續錄製，按「停止錄製」結束本場": "Power output is off; sensors keep recording; press \"Stop recording\" to end this run",
    "數據儲存失敗，詳細內容見實驗 log": "Failed to save the data; see the experiment log for details",
    "監測已停止，但電源仍在輸出中：請確認是否要關閉輸出": "Monitoring stopped, but the power supply is still outputting: check whether the output should be turned off",
    "沒有錄製到音訊數據": "No audio data was recorded",
    "寫檔失敗": "File write failed",
    "溫度監測中斷": "Temperature monitoring interrupted",
    "音訊初始化失敗": "Audio initialization failed", "距離初始化失敗": "Distance initialization failed",
    "光譜儀初始化失敗": "Spectrometer initialization failed", "頻譜 CSV 產生失敗": "Spectrogram CSV generation failed",
    "溫度：監測執行緒意外中止，之後不會再有溫度數據（例外與 traceback 見實驗 log）": "Temperature: the monitoring thread stopped unexpectedly; there will be no more temperature data (exception and traceback in the experiment log)",
    "音訊：初始化失敗，本次實驗沒有音訊數據；其他感測器照常（原因與建議見狀態卡）": "Audio: initialization failed; this run has no audio data; the other sensors continue normally (cause and advice on the status card)",
    "距離：測距儀初始化失敗，本次實驗沒有距離數據；其他感測器照常（原因與建議見狀態卡）": "Distance: rangefinder initialization failed; this run has no distance data; the other sensors continue normally (cause and advice on the status card)",
    "光譜儀：初始化失敗，本次實驗沒有光譜數據；其他感測器照常（原因與建議見狀態卡）": "Spectrometer: initialization failed; this run has no spectrum data; the other sensors continue normally (cause and advice on the status card)",
    "遠端只能瀏覽實驗紀錄；其他功能請在接儀器的電腦上操作": "Remote access can only browse the experiment history; use the computer connected to the instruments for everything else",
    "輸入框的設定值還沒套用": "The values in the input boxes have not been applied",
    "開始前必須確認輸出高壓（confirm_output）": "High-voltage output must be confirmed before starting (confirm_output)",
    "請先按「檢驗Sensor與電供」": "Press \"Check sensors & PSU\" first",
    "上次檢驗電源不正常，請排除後重新「檢驗Sensor與電供」": "The power supply was not OK at the last check; fix it and run \"Check sensors & PSU\" again",
    "電源參數或實驗時間和檢驗時不同，請重新「檢驗Sensor與電供」": "The power parameters or experiment duration differ from the check; run \"Check sensors & PSU\" again",
    "電源未連線：請重新「檢驗Sensor與電供」": "Power supply not connected: run \"Check sensors & PSU\" again",
    "正在「檢驗Sensor與電供」（寫入並讀回電源參數中），請稍候再操作電源": "\"Check sensors & PSU\" is in progress (writing and reading back the power parameters); wait before operating the power supply",
    "參數會寫入電源的 EEPROM，請先關閉輸出再修改": "Parameters are written to the power supply's EEPROM; turn the output off before changing them",
    "電源輸出已開啟，取消寫入": "Power output is on; write cancelled",
    "電壓上限與電流上限都必須大於 0，否則電源不會輸出": "Voltage limit and current limit must both be greater than 0, otherwise the power supply produces no output",
    "控制方式必須是 cv（定電壓）或 cc（定電流）": "Control mode must be cv (constant voltage) or cc (constant current)",
    "電源不是 RS232 控制：網頁無法控制，請確認電源面板的控制來源": "The power supply is not under RS232 control: the web page cannot control it; check the control source on the power supply panel",
    "電源未就緒（Ready = 0）": "Power supply not ready (Ready = 0)",
    "Interlock 觸發中（上蓋開關或安全迴路），請檢查安全迴路": "Interlock is active (cover switch or safety loop); check the safety loop",
    "尚未收到電源狀態": "No power supply status received yet",
    "實驗時間為必填（分:秒 或 秒）": "Experiment duration is required (min:sec or seconds)",
    "無法建立存檔資料夾": "Could not create the data folder",
    "需要至少": "At least",
    "未選擇有效的音訊設備，音訊監測將被停用": "No valid audio device selected; audio monitoring will be disabled",
    "系統找不到任何音訊輸入設備": "The system found no audio input device",
    "未選擇音訊設備": "No audio device selected", "音訊設備清單讀取失敗": "Could not read the audio device list",
    "至少需要啟用一個感測器": "At least one sensor must be enabled",
    "（未填標籤）": "(no labels)", "舊格式": "Legacy format",
    "找不到這筆實驗": "Experiment not found", "找不到這筆實驗或資料種類": "Experiment or data kind not found",
    "這筆實驗沒有這種資料": "This experiment has no such data", "找不到檔案": "File not found", "找不到 Absolute 欄": "Absolute column not found",

    // ---- 伺服器：感測器診斷建議（狀態卡） ----
    "找不到 drivers/LKIF2.dll：確認 drivers 資料夾內有 LKIF2.dll、CmnLib.dll、KeyUsbDrv.dll": "drivers/LKIF2.dll not found: make sure the drivers folder contains LKIF2.dll, CmnLib.dll and KeyUsbDrv.dll",
    "此系統不是 Windows，無法載入 KEYENCE DLL": "This system is not Windows; the KEYENCE DLL cannot be loaded",
    "DLL 載入失敗：缺少相依 DLL（CmnLib.dll／KeyUsbDrv.dll）或 Python 與 DLL 位元（32/64）不符": "DLL failed to load: a dependent DLL (CmnLib.dll / KeyUsbDrv.dll) is missing, or Python and the DLL differ in bitness (32/64)",
    "LK-G5000 未連線：檢查 USB 線、控制器電源、KEYENCE USB 驅動": "LK-G5000 not connected: check the USB cable, controller power and KEYENCE USB driver",
    "測距儀發生未預期的錯誤：詳細內容見實驗 log": "Unexpected rangefinder error: see the experiment log for details",
    "缺少 PyQt5：光譜儀驅動需要 PyQt5（uv sync --extra spectrometer）": "PyQt5 missing: the spectrometer driver needs PyQt5 (uv sync --extra spectrometer)",
    "光譜儀驅動載入失敗：詳細內容見實驗 log": "Spectrometer driver failed to load: see the experiment log for details",
    "找不到 AvaSpec 驅動檔：Windows 需要 drivers/avaspecx64.dll，macOS／Linux 需安裝 AvaSpec 原生函式庫": "AvaSpec driver file not found: Windows needs drivers/avaspecx64.dll; macOS / Linux need the AvaSpec native library installed",
    "AvaSpec DLL 載入失敗：缺少相依檔，或 Python 與 DLL 位元（32/64）不符": "AvaSpec DLL failed to load: a dependency is missing, or Python and the DLL differ in bitness (32/64)",
    "此系統無法載入 AvaSpec 驅動": "This system cannot load the AvaSpec driver",
    "找不到光譜儀：檢查 USB 線、電源與 AvaSpec 驅動（裝置管理員是否看得到 AvaSpec）": "Spectrometer not found: check the USB cable, power and AvaSpec driver (is AvaSpec visible in Device Manager?)",
    "光譜儀沒有回傳資料（等待超時）：確認光譜儀未被其他程式佔用，並檢查積分時間與觸發設定": "The spectrometer returned no data (wait timed out): make sure no other program is using it, and check the integration time and trigger settings",
    "光譜儀發生未預期的錯誤：詳細內容見實驗 log": "Unexpected spectrometer error: see the experiment log for details",
    "控制器尚未送出量測值（WAITING）：剛開始量測的前一兩筆屬正常；持續出現時確認控制器在量測模式": "The controller has not sent a reading yet (WAITING): normal for the first one or two readings; if it persists, make sure the controller is in measurement mode",
    "目標超出量測範圍（過遠，+RANGEOVER）：調整感測頭與目標的距離": "Target out of measuring range (too far, +RANGEOVER): adjust the distance between sensor head and target",
    "目標超出量測範圍（過近，-RANGEOVER）：調整感測頭與目標的距離": "Target out of measuring range (too close, -RANGEOVER): adjust the distance between sensor head and target",
    "收不到反射光（ALARM）：檢查距離、角度、表面反光，或換 HEAD": "No reflected light received (ALARM): check distance, angle and surface reflectivity, or change the HEAD",
    "未選擇有效的音訊輸入設備": "No valid audio input device selected",
    "音訊系統（PortAudio/PyAudio）初始化失敗：確認音訊驅動與 PyAudio 已正確安裝": "Audio system (PortAudio/PyAudio) failed to initialize: make sure the audio driver and PyAudio are installed correctly",
    "系統找不到任何音訊輸入設備：確認麥克風已插上且未被系統停用": "The system found no audio input device: make sure the microphone is plugged in and not disabled by the system",
    "設備無法開啟：可能被其他程式佔用，或未授予麥克風權限": "The device could not be opened: it may be in use by another program, or microphone permission was not granted",
    "連續 3 秒錄到的訊號全為 0：麥克風權限未開、設備靜音或未接；檢查 Windows 隱私權設定與麥克風開關": "The signal has been all zeros for 3 seconds: microphone permission is off, the device is muted or not connected; check the Windows privacy settings and the microphone switch",
    "量測值無效": "Invalid reading",
    "溫度計單位是 °F：請在 TX-600N 切回 °C": "The thermometer unit is °F: switch the TX-600N back to °C",
    "上次套用配方沒有完成讀回比對（部分參數可能已寫入）": "The last recipe application did not finish the readback comparison (some parameters may have been written)",
    "讀不到": "unreadable",
    "原始 counts（未扣暗光）": "Raw counts (dark not subtracted)",
    "資料夾:": "Folder:",
    "伺服器關閉中": "Server shutting down",
    "電弧": "Arc",
    "Test version 韌體訊息": "Test version firmware message",
    "原因不明": "Unknown cause",
    "無": "none",

    // ---- 實驗紀錄頁 ----
    "實驗列表": "Experiment list",
    "搜尋標籤或實驗 ID": "Search label or experiment ID", "搜尋實驗": "Search experiments",
    "重新整理列表": "Refresh list", "載入中…": "Loading…",
    "從左側選一筆實驗，就會把它的電源、距離、溫度和光譜儀數據畫出來。": "Pick an experiment on the left to plot its power supply, distance, temperature and spectrometer data.",
    "重設縮放": "Reset zoom", "重新載入": "Reload",
    "在圖上拖曳選取時間範圍可放大，雙擊重設。所有圖共用同一條時間軸，單位是距實驗開始的秒數。藍底表示電源輸出中。": "Drag on a chart to zoom into a time range; double-click to reset. All charts share one time axis, in seconds since the start of the experiment. Blue background means power output is on.",
    "原始檔案（": "Raw files (", "實驗中的錯誤紀錄（": "Errors during the experiment (",
    "）": ")",
    "電壓": "Voltage", "功率": "Power", "電弧率": "Arc rate", "次/s": "/s",
    "A 極平均": "A pole avg.", "B 極平均": "B pole avg.", "A 極": "A pole", "B 極": "B pole",
    "電壓上限設定": "Voltage limit setting", "電流上限設定（RMS）": "Current limit setting (RMS)",
    "距離（測距儀）": "Distance (rangefinder)", "全波段總強度": "Total intensity, all wavelengths", "總強度": "Total intensity",
    "電": "P", "距": "D", "溫": "T", "光": "S", "音": "A",
"中斷（未正常結束）": "Interrupted (did not end normally)",
    "原始資料已存（頻譜未產生）": "Raw data saved (spectrogram not generated)",
    "中止（原因見結果）": "Aborted (see result for reason)",
    "光譜熱圖（波長 × 時間，counts）": "Spectrum heatmap (wavelength × time, counts)",
    "點一下圖上的位置，下方會顯示那一刻的完整光譜": "Click a position on the chart to show the full spectrum at that moment below",
    "資料不足，無法畫熱圖": "Not enough data to draw the heatmap",
    "電源輸出時間": "Power output time", "最高電壓（A / B 極）": "Max voltage (A / B pole)",
    "最大電流（A / B 極）": "Max current (A / B pole)", "最大電弧率": "Max arc rate",
    "調節模式（輸出中）": "Regulation mode (output on)", "距離範圍": "Distance range", "溫度範圍": "Temperature range",
    "溫度讀取失敗": "Temperature read failures", "距離無效讀值": "Invalid distance readings",
    "模擬資料": "Simulated data",
    "這筆實驗有音訊檔（WAV），這一版還不能在頁面上畫成圖，可以從下方的原始檔案下載。": "This experiment has an audio file (WAV). It cannot be plotted on this page yet; download it from the raw files below.",
    "載入失敗": "Failed to load",
    "還沒有任何實驗紀錄": "No experiments recorded yet",
    "圓": "round", "方形": "square",
  };

  // ---------------------------------------------------------------------------------------------
  // 句型（$n 會先翻譯再代入）。越具體的放越前面。
  // ---------------------------------------------------------------------------------------------
  const P = (re, to) => [re, to];
  const PATTERNS = [
    // ---- 時間 ----
    P(/^(\d+) 秒前$/, "$1 s ago"),
    P(/^(\d+) 分 (\d+) 秒前$/, "$1 min $2 s ago"),
    P(/^(\d+) 小時 (\d+) 分前$/, "$1 h $2 min ago"),
    P(/^(\d+) 時 (\d+) 分 (\d+) 秒$/, "$1 h $2 min $3 s"),
    P(/^(\d+) 分 (\d+) 秒$/, "$1 min $2 s"),
    P(/^([\d.]+) 秒$/, "$1 s"),
    P(/^([\d.]+) 小時$/, "$1 h"),
    P(/^最後正常 (.+)$/, "Last OK $1"),
    P(/^連續失敗 (\d+) 次$/, "$1 consecutive failures"),
    // ---- 圖表備註 / 狀態列 ----
    P(/^(.+) · 每秒更新$/, "$1 · updates every second"),
    P(/^模式 (.+)$/, "Mode $1"),
    P(/^正 (.+)$/, "Fwd $1"),
    P(/^負 (.+)$/, "Rev $1"),
    P(/^輸出 (-?[\d.].*)$/, "Out $1"),
    P(/^積分 (.+)$/, "Integration $1"),
    P(/^每 ([\d.]+) 秒$/, "every $1 s"),
    P(/^顯示 ([\d.]+) 秒$/, "showing $1 s"),
    // 頂列模擬標籤：英文名稱縮短，避免擠到標題
    P(/^模擬模式：(.+?)(（含故障）)?$/, (m) => "Simulated: " + m[1].split("、").map((k) => ({ 溫度: "Temp", 距離: "Dist", 音訊: "Audio", 光譜儀: "Spec", 電源: "PSU" })[k] || tr(k)).join(", ") + (m[2] ? " (with faults)" : "")),
    P(/^列舉失敗：(.+)$/, "Enumeration failed: $1"),
    P(/^找到 (\d+) 個候選埠，可直接輸入$/, "Found $1 candidate port(s); you can type one in"),
    P(/^(\d+) 秒後開啟輸出（計時從輸出開啟算起）$/, "Output turns on in $1 s (the timer starts when output turns on)"),
    P(/^(\d+) 秒後開啟輸出$/, "Output turns on in $1 s"),
    P(/^輸出中 · (.+)$/, "Output on · $1"),
    P(/^輸出中 (\d\d:\d\d)$/, "Output on $1"),
    P(/^錄製基線中 · (.+)$/, "Recording baseline · $1"),
    P(/^電壓 ([\d.—]+) V$/, "Voltage $1 V"),
    P(/^電流 ([\d.—]+) A$/, "Current $1 A"),
    P(/^(.+) · 只能結束這場$/, "$1 · this run can only be ended"),
    P(/^上一場 (.+)：(.+)$/, "Previous run $1: $2"),
    P(/^缺少感測器：(.+)$/, "Missing sensors: $1"),
    P(/^缺少感測器 (.+)$/, "Missing sensors $1"),
    P(/^全部正常（試收 (\S+) 秒），可以開始$/, "All OK ($1 s trial collection); ready to start"),
    P(/^正常（有效數據 (\d+) 筆）$/, "OK ($1 valid samples)"),
    P(/^ch(\d+) = (.+)$/, "ch$1 = $2"),
    P(/^電源回報實際頻率 f ([\d.\-—]+) Hz、實際佔空比 ([\d.\-—]+) %$/, "actual frequency f reported by the supply $1 Hz, actual duty cycle $2 %"),
    P(/^請到頂列「⚙ 設定」檢查(.*)$/, "Check it in \"⚙ Settings\" in the top bar: $1"),
    P(/^「電源 COM 埠」「裝置」$/, "\"Power supply COM port\", \"Devices\""),
    P(/^「電源 COM 埠」$/, "\"Power supply COM port\""),
    P(/^「裝置」$/, "\"Devices\""),
    // ---- 標籤 / 參數檢查 ----
    P(/^試片(.+)為必填$/, "Specimen $1 is required"),
    P(/^試片(.+)必須是大於 0 的數字$/, "Specimen $1 must be a number greater than 0"),
    P(/^(.+)為必填$/, "$1 is required"),
    P(/^(.+)須大於 (\S+)、不超過(\S+) (\S+)$/, "$1 must be greater than $2 and at most $3 $4"),
    P(/^(.+)須介於 (\S+)–(\S+) (\S+)$/, "$1 must be between $2 and $3 $4"),
    P(/^電流不限（設為電源上限 (\S+) A），由功率上限保護$/, "No current limit (set to the supply maximum of $1 A); protected by the power limit"),
    P(/^電壓不限（設為電源上限 (\S+) V），由功率上限保護$/, "No voltage limit (set to the supply maximum of $1 V); protected by the power limit"),
    P(/^定電壓 (.+)$/, "Constant voltage $1"),
    P(/^定電流 (.+)$/, "Constant current $1"),
    P(/^圓形 (.+)$/, "Round $1"),
    P(/^厚 (.+)$/, "thickness $1"),
    P(/^請輸入 (\S+)–(\S+) 的數值$/, "Enter a value from $1 to $2"),
    P(/^請輸入正向／負向佔空比 %$/, "Enter the forward / reverse duty cycle in %"),
    P(/^正向＋負向合計要是整數（目前 (\S+) %）：Off Time 是整數 %$/, "Forward + reverse must total a whole number (now $1 %): Off Time is a whole number of %"),
    P(/^正向＋負向合計要在 2–99 %（目前 (\S+) %）：Off Time 為 1–98 %$/, "Forward + reverse must total 2–99 % (now $1 %): Off Time is 1–98 %"),
    P(/^換算後占空比 (\S+) % 超出 1–99$/, "Converted duty cycle $1 % is outside 1–99"),
    P(/^停止時間 (\S+) % 超過已實測的 45 %：尚未實測驗證$/, "Off time $1 % exceeds the tested 45 %: not verified yet"),
    P(/^超出手冊範圍（0–20 %），已在 2026-11-02 實測到 45 %$/, "Beyond the manual's range (0–20 %); tested up to 45 % on 2026-11-02"),
    P(/^(.+)；會切換到低頻模式，未在真機驗證$/, "$1; switches to low-frequency mode, not verified on real hardware"),
    P(/^會切換到低頻模式，未在真機驗證$/, "Switches to low-frequency mode, not verified on real hardware"),
    P(/^(\d+)–(\d+) Hz；(\d+) Hz 以下自動使用低頻模式$/, "$1–$2 Hz; low-frequency mode is used automatically at or below $3 Hz"),
    P(/^([\d–]+) Hz；1000 Hz 以下自動使用低頻模式$/, "$1 Hz; low-frequency mode is used automatically at or below 1000 Hz"),
    P(/^1–100000 Hz；1000 Hz 以下自動使用低頻模式$/, "1–100000 Hz; low-frequency mode is used automatically at or below 1000 Hz"),
    P(/^佔空比 = 該方向通電時間佔一個週期的比例。每個週期 (.+)：正向通電 (.+) → 停 (.+) → 負向通電 (.+) → 停 (.+)$/,
      "Duty cycle = share of a period that this direction conducts. Each period $1: forward on $2 → off $3 → reverse on $4 → off $5"),
    P(/^電源目前存的停止時間 (\S+) % 超出可用範圍 1–98 %，檢驗時會改寫$/, "The off time currently stored in the supply ($1 %) is outside the usable range 1–98 %; it will be rewritten during the check"),
    P(/^電源目前是 (.+) 模式（不在單極／雙極選項），套用後會改成所選模式$/, "The supply is currently in $1 mode (not a unipolar / bipolar option); applying will switch it to the selected mode"),
    P(/^功率上限必須大於 0，否則電源不會輸出$/, "Power limit must be greater than 0, otherwise the supply produces no output"),
    P(/^尚未取得設定範圍$/, "Setting range not available yet"),
    P(/^為 0 時不會有輸出$/, "No output when set to 0"),
    // ---- 電源訊息 / 摘要 ----
    P(/^總功率 (\S+) kW · 調節 (.+) · 實際頻率 f (\S+) Hz · 實際佔空比 (\S+) % · 電弧 (\S+) 次\/秒$/,
      "Total power $1 kW · regulation $2 · actual frequency f $3 Hz · actual duty cycle $4 % · arcs $5 /s"),
    P(/^電源訊息 (\S+)（Test version 家族，只記錄）：(.*)$/, "Power supply message $1 (Test version family, logged only): $2"),
    P(/^(警報|警告) (\S+)：(.+?)（原文：(.+)）(?:：(.+))?$/, "$1 $2: $3 (original: $4)$5"),
    P(/^警報中（讀取警報代碼中…）$/, "Alarm active (reading alarm code…)"),
    P(/^警告中（讀取警報代碼中…）$/, "Warning active (reading alarm code…)"),
    P(/^無$/, "none"),
    P(/^(\d+) 個，共 (.+)$/, "$1 file(s), $2 in total"),
    P(/^電源目前「(.+)」，參數只能在輸出關閉時寫入$/, "The power supply is currently \"$1\"; parameters can only be written while output is off"),
    P(/^電源目前「(.+)」，開始前輸出必須是關閉的$/, "The power supply is currently \"$1\"; output must be off before starting"),
    P(/^電源通訊不穩：最近 (\d+) 次沒有回應（(.+)）$/, "Power supply communication unstable: no response for the last $1 attempts ($2)"),
    P(/^電源連線失敗：(.+)$/, "Power supply connection failed: $1"),
    P(/^無法連線電源（(.+)）$/, "Cannot connect to the power supply ($1)"),
    P(/^電源沒有回應 heartbeat（(.+)）：確認電源開機、RS232\/USB 轉接線$/, "The power supply did not answer the heartbeat ($1): check that it is powered on and the RS232 / USB adapter cable"),
    P(/^電源參數錯誤：(.+)$/, "Power parameter error: $1"),
    P(/^電源參數寫入後讀回不一致，不能開啟輸出：(.+)$/, "Power parameters read back differently after writing; output cannot be turned on: $1"),
    P(/^上次套用的電源參數讀回不一致，請重新套用並確認讀回一致：(.+)$/, "The last applied power parameters read back differently; apply them again and confirm they match: $1"),
    P(/^電壓設定必須大於 0$/, "Voltage setpoint must be greater than 0"),
    P(/^電流設定必須大於 0$/, "Current setpoint must be greater than 0"),
    P(/^(電壓上限|電流上限|功率上限)必須大於 0$/, "$1 must be greater than 0"),
    P(/^on 必須是 true 或 false$/, "on must be true or false"),
    P(/^channel 必須是整數$/, "channel must be an integer"),
    P(/^實驗時間格式錯誤：(.+)（請輸入 分:秒，例如 10:00，或秒數）$/, "Invalid experiment duration: $1 (enter min:sec such as 10:00, or seconds)"),
    P(/^實驗時間必須大於 0 且不超過 (.+)$/, "Experiment duration must be greater than 0 and at most $1"),
    P(/^檢驗結果已超過 (\d+) 分鐘，請重新「檢驗Sensor與電供」$/, "The check result is older than $1 minutes; run \"Check sensors & PSU\" again"),
    P(/^檢驗結果已超過 5 分鐘，請重新「檢驗Sensor與電供」$/, "The check result is older than 5 minutes; run \"Check sensors & PSU\" again"),
    P(/^檢驗失敗 \(HTTP (\d+)\)$/, "Check failed (HTTP $1)"),
    P(/^檢驗失敗: (.+)$/, "Check failed: $1"),
    P(/^開始實驗失敗 \(HTTP (\d+)\)$/, "Failed to start the experiment (HTTP $1)"),
    P(/^開始實驗失敗: (.+)$/, "Failed to start the experiment: $1"),
    P(/^啟動測試失敗 \(HTTP (\d+)\)$/, "Failed to start the test (HTTP $1)"),
    P(/^啟動測試失敗: (.+)$/, "Failed to start the test: $1"),
    P(/^模式：(.+)$/, "Mode: $1"),
    P(/^頻率 f (\S+) Hz · 正向佔空比 (\S+) % · 負向佔空比 (\S+) %$/, "Frequency f $1 Hz · forward duty cycle $2 % · reverse duty cycle $3 %"),
    P(/^頻率 f (\S+) Hz · 佔空比 (\S+) %$/, "Frequency f $1 Hz · duty cycle $2 %"),
    P(/^控制方式：定電壓（電壓設定 (\S+) V，電流不限＝(\S+) A）$/, "Control mode: constant voltage (voltage setpoint $1 V, no current limit = $2 A)"),
    P(/^控制方式：定電流（電流設定 (\S+) A，電壓不限＝(\S+) V）$/, "Control mode: constant current (current setpoint $1 A, no voltage limit = $2 V)"),
    P(/^注意：(.+)$/, "Note: $1"),
    P(/^功率上限 (\S+) kW$/, "Power limit $1 kW"),
    P(/^電壓設定 (\S+) V$/, "Voltage setpoint $1 V"),
    P(/^電流設定 (\S+) A$/, "Current setpoint $1 A"),
    P(/^實驗時間：(.+)$/, "Experiment duration: $1"),
    P(/^先錄 (\S+) 秒基線（電源關閉）才開啟輸出；時間到自動關閉輸出並停止錄製$/, "Records a $1 s baseline (power off) before turning the output on; when the time is up, the output is turned off and recording stops automatically"),
    P(/^(.+?) ch(\d+)：(.+) → (.+)（會寫入電源 EEPROM）$/, "$1 ch$2: $3 → $4 (will be written to the power supply EEPROM)"),
    P(/^(.+)：請先輸入要寫入的值$/, "$1: enter the value to write first"),
    P(/^(.+?)：(.+) → (.+)$/, "$1: $2 → $3"),
    // ---- 伺服器通知 ----
    P(/^(.+)異常$/, "$1 problem"),
    P(/^(.+)已恢復$/, "$1 recovered"),
    P(/^(.+?)：(.+?)，(?:連續 (\d+) 次、)?已 (\d+) 秒，持續重試中（原因與建議見狀態卡）$/, "$1: $2, $3 consecutive failures, for $4 s, still retrying (cause and advice on the status card)"),
    P(/^(.+)已恢復正常（失敗 (\d+) 次、持續 (\d+) 秒）$/, "$1 is back to normal ($2 failures, lasted $3 s)"),
    P(/^(.+) 資料寫入失敗（磁碟可能已滿或被拔除）。\n監測仍在進行，請盡快釋放空間或停止監測。$/, "$1 data could not be written (the disk may be full or removed).\nMonitoring continues; free up space or stop monitoring soon."),
    P(/^(.+)：(.+)\n感測器繼續錄製，請按「結束這場」存檔$/, "$1: $2\nSensors keep recording; press \"End this run\" to save"),
    P(/^監測完成！\n([\s\S]*)$/, "Monitoring finished!\n$1"),
    P(/^原始音訊已保存於 (.+)，可稍後執行：\n([\s\S]+)$/, "The raw audio was saved as $1; you can run later:\n$2"),
    P(/^數據已儲存至: (.+)$/, "Data saved to: $1"),
    P(/^資料夾: (.+)$/, "Folder: $1"),
    P(/^音訊 (.+)$/, "Audio $1"),
    P(/^(.+) · (.+)$/, "$1 · $2"),
    // ---- 感測器診斷（帶變數） ----
    P(/^讀值函式回傳通訊層錯誤 (.+)（(.+)）：USB 可能中斷；檢查 USB 線與控制器電源$/, "Read function returned communication-layer error $1 ($2): USB may have disconnected; check the USB cable and controller power"),
    P(/^控制器回報錯誤 (.+)（(.+)）：持續出現時確認控制器設定，或停止後重新開始量測$/, "Controller reported error $1 ($2): if it persists, check the controller settings, or stop and start measuring again"),
    P(/^讀值時發生程式例外（(.+)）：不是儀器回報的錯誤碼；持續發生時 traceback 會寫進實驗 log$/, "Program exception while reading ($1): not an error code reported by the instrument; if it persists the traceback is written to the experiment log"),
    P(/^讀取溫度時發生程式例外（(.+)）：不是溫度計回報的錯誤；持續發生時 traceback 會寫進實驗 log$/, "Program exception while reading temperature ($1): not an error reported by the thermometer; if it persists the traceback is written to the experiment log"),
    P(/^錄音時發生錯誤（(.+)）：檢查麥克風是否被拔除或被其他程式佔用；持續發生時 traceback 會寫進實驗 log$/, "Error while recording ($1): check whether the microphone was unplugged or is used by another program; if it persists the traceback is written to the experiment log"),
    P(/^量測值無效（(.+)）：檢查目標表面與感測頭設定$/, "Invalid reading ($1): check the target surface and sensor head settings"),
    P(/^(\d+) 秒內沒有收到有效數據：檢查接線、電源與驅動$/, "No valid data received within $1 s: check wiring, power and driver"),
    P(/^(\d+) 秒內沒有收到有效數據$/, "No valid data received within $1 s"),
    P(/^(\d+) 秒內錄到的訊號全為 0：麥克風權限未開、設備靜音或未接；檢查 Windows 隱私權設定與麥克風開關$/, "The signal was all zeros for $1 s: microphone permission is off, the device is muted or not connected; check the Windows privacy settings and the microphone switch"),
    P(/^音訊系統（PortAudio\/PyAudio）初始化失敗: (.+)$/, "Audio system (PortAudio/PyAudio) failed to initialize: $1"),
    P(/^指定的設備 (.+) 不存在或不是輸入設備：麥克風可能已被拔除，請重新整理設備清單$/, "The specified device $1 does not exist or is not an input device: the microphone may have been unplugged; refresh the device list"),
    P(/^查詢音訊設備時發生錯誤：(.+)$/, "Error while querying audio devices: $1"),
    P(/^音訊錄製器初始化失敗（AudioRecorder 結束碼 (.+)），請檢查設備是否被佔用或麥克風權限$/, "Audio recorder failed to initialize (AudioRecorder exit code $1); check whether the device is in use or microphone permission is missing"),
    P(/^熱電偶類型設成 (.+) 型：LP 測棒是 K 型，請在 TX-600N 切回 K$/, "Thermocouple type is set to $1: the LP probe is type K; switch the TX-600N back to K"),
    P(/^(.+) Err（探棒未接、斷線或超出量程）$/, "$1 Err (probe not connected, broken, or out of range)"),
    P(/^(.+) 讀值 (.+) 超出 K 型量程$/, "$1 reading $2 is outside the type K range"),
    P(/^參數輸入錯誤: (.+)$/, "Invalid parameter: $1"),
    P(/^實驗標籤未填: (.+)$/, "Experiment label missing: $1"),
    P(/^實驗標籤錯誤: (.+)$/, "Experiment label error: $1"),
    P(/^採樣率必須大於 0$/, "Sample rate must be greater than 0"),
    P(/^更新間隔必須大於 0$/, "Update interval must be greater than 0"),
    P(/^測距間隔必須大於 0$/, "Measurement interval must be greater than 0"),
    P(/^測距儀模式必須是 0 或 1$/, "Rangefinder mode must be 0 or 1"),
    P(/^光譜儀量測間隔必須大於 0$/, "Spectrometer measurement interval must be greater than 0"),
    P(/^光譜儀積分時間必須介於 0 與 10000 毫秒之間$/, "Spectrometer integration time must be between 0 and 10000 ms"),
    P(/^頻譜圖顯示長度必須介於 0 與 (\d+) 秒之間（只影響畫面，存檔一律保存全程）$/, "Spectrogram display length must be between 0 and $1 s (affects the display only; saved files always keep the whole recording)"),
    P(/^試片形狀須為正方形或圓形$/, "Specimen shape must be square or round"),
    P(/^試片（(.+)）為必填$/, "Specimen ($1) is required"),
    P(/^試片(.+)必須是數字$/, "Specimen $1 must be a number"),
    P(/^試片(.+)必須大於 0$/, "Specimen $1 must be greater than 0"),
    P(/^自動偵測：溫度計在 (.+)（已探測 (.+)）$/, "Auto-detect: thermometer on $1 (probed $2)"),
    P(/^自動偵測找不到溫度計：已探測 (.+)，都沒有正確回應；檢查感測器電源、RS485 A\/B 接線、站號\(0x03\)\/鮑率\(57600\)，或手動輸入 COM 埠$/, "Auto-detect found no thermometer: probed $1, none answered correctly; check sensor power, RS485 A/B wiring, station number (0x03) / baud rate (57600), or enter the COM port manually"),
    P(/^自動偵測找不到任何 USB 轉序列埠：USB-RS485 轉接器未插上，或驅動未安裝$/, "Auto-detect found no USB serial port: the USB-RS485 adapter is not plugged in or its driver is not installed"),
    P(/^• 存檔磁碟剩餘空間只有 (.+)$/, "• Only $1 of disk space left for saving data"),
    P(/^• 測距儀連接失敗，距離監測將被停用$/, "• Rangefinder connection failed; distance monitoring will be disabled"),
    P(/^• 測距儀初始化失敗，距離監測將被停用$/, "• Rangefinder initialization failed; distance monitoring will be disabled"),
    P(/^• 光譜儀初始化失敗，光譜儀監測將被停用$/, "• Spectrometer initialization failed; spectrometer monitoring will be disabled"),
    P(/^• 未選擇有效的音訊設備，音訊監測將被停用$/, "• No valid audio device selected; audio monitoring will be disabled"),
    P(/^(.+)，溫度監測將被停用$/, "$1; temperature monitoring will be disabled"),
    P(/^• (.+)，溫度監測將被停用$/, "• $1; temperature monitoring will be disabled"),
    P(/^電源沒有回應（逾時）$/, "Power supply did not respond (timeout)"),
    P(/^與電源通訊中斷（(.+)）；電源會在 4–5 秒內自行關閉輸出（ActSourceFail）$/, "Communication with the power supply lost ($1); it will turn its output off by itself within 4–5 s (ActSourceFail)"),
    P(/^電源警報，輸出已關閉(?:：(.+))?$/, "Power alarm, output turned off$1"),
    P(/^電源回報輸出已關閉：(.+)$/, "The power supply reported output off: $1"),
    P(/^電源無法開啟輸出：(.+)$/, "The power supply could not turn the output on: $1"),
    P(/^Interlock 觸發（上蓋開關或安全迴路被打開），電源已關閉輸出$/, "Interlock triggered (cover switch or safety loop opened); the power supply turned its output off"),
    // ---- 實驗紀錄頁 ----
    P(/^(.+)（(.+)）$/, "$1 ($2)"),
    P(/^共 (\d+) 筆，符合 (\d+) 筆 · (.+)$/, "$1 experiments, $2 match · $3"),
    P(/^共 (\d+) 筆 · (.+)$/, "$1 experiments · $2"),
    P(/^載入列表失敗：(.+)$/, "Failed to load the list: $1"),
    P(/^讀取失敗：(.+)$/, "Read failed: $1"),
    P(/^讀取光譜失敗：(.+)$/, "Failed to read the spectrum: $1"),
    P(/^(\S+) s 的光譜（counts）$/, "Spectrum at $1 s (counts)"),
    P(/^時長 (.+)$/, "Duration $1"),
    P(/^電源模式 (.+)$/, "Power mode $1"),
    P(/^試片 (圓形|方形)$/, "Specimen $1"),
    P(/^這筆實驗沒有(.+)資料$/, "This experiment has no $1 data"),
    P(/^(\d+) \/ (\d+) 筆$/, "$1 / $2 samples"),
    P(/^(\S+) ~ (\S+) (\S+)$/, "$1 ~ $2 $3"),
    P(/^(\S+) \/ (\S+) V$/, "$1 / $2 V"),
    P(/^(\S+) 次\/s$/, "$1 /s"),
    P(/^(\S+)\u3000(.+)$/, "$1  $2"),
  ];


  // ---- 電源驅動（sensors/truplasma_package）回報的文字：相態、事件、參數名稱、警報 ----
  Object.assign(EN, {
    "bit0 反向脈衝": "bit0 reverse pulse", "bit1 硬電弧後軟啟動": "bit1 soft start after hard arc",
    "bit0 反向脈衝、bit1 硬電弧後軟啟動": "bit0 reverse pulse, bit1 soft start after hard arc",
    "開始輸出前使用者結束（未開啟輸出）": "Ended by the user before output was turned on (output never turned on)",
    "結束中（未開啟輸出）": "Ending (output was never turned on)",
    "輸出已關閉，停止錄製並存檔": "Output is off; stopping the recording and saving",
    "電源輸出與主繼電器已關閉，停止錄製": "Power output and main relay are off; stopping the recording",
    "錄製在輸出中被外部停止，已關閉輸出": "Recording was stopped externally while output was on; output turned off",
    "錄製在開始輸出前被外部停止": "Recording was stopped externally before output was turned on",
    "接通主繼電器、等待電源就緒": "Closing the main relay, waiting for the supply to be ready",
    "已緊急停止：電源輸出已關閉，感測器繼續錄製；按「停止錄製」結束本場": "Emergency stopped: power output is off, sensors keep recording; press \"Stop recording\" to end this run",
    "收到停止請求：": "Stop request received: ",
    "輸出關閉": "Output off", "主繼電器接通中": "Main relay closing", "關閉輸出中": "Turning output off",
    "開始開啟輸出：接通主繼電器": "Turning output on: closing the main relay", "關閉輸出": "Turn output off",
    "緊急關閉輸出": "Emergency output shutdown", "主繼電器已接通，開啟輸出": "Main relay closed, turning output on",
    "輸出已開啟": "Output is on", "輸出已關閉": "Output is off", "關閉輸出逾時，已同時切斷主繼電器": "Turn-off timed out; the main relay was also opened",
    "電源回報輸出已關閉（INHIBIT），已停止": "The supply reported output off (INHIBIT); stopped",
    "電源警報，輸出已關閉（警報代碼見下一行）": "Power alarm, output turned off (alarm code on the next line)",
    "Interlock 觸發，輸出已關閉": "Interlock triggered, output turned off",
    "電源有警報，請先排除原因並重設警報": "The power supply has an alarm; fix the cause and reset the alarm first",
    "電源沒有附文字": "The supply gave no text", "請先關閉輸出再重設警報": "Turn the output off before resetting the alarm",
    "程式沒有取得 RS232 控制權，請確認面板上的控制來源": "The program did not get RS232 control; check the control source on the panel",
    "電壓、電流、功率設定值都是 0": "Voltage, current and power setpoints are all 0",
    "電源連線已關閉": "Power supply connection closed", "輸出已開啟，取消寫入": "Output is on; write cancelled",
    "脈衝模式": "Pulse mode", "頻率設定值": "Frequency setpoint", "佔空比設定值": "Duty cycle setpoint", "低頻模式": "Low-frequency mode",
    "低頻設定值": "Low-frequency setpoint", "上升時間 A": "Rise time A", "下降時間 A": "Fall time A", "上升時間 B": "Rise time B", "下降時間 B": "Fall time B",
    "功率平衡（Power Equalizer）": "Power balance (Power Equalizer)", "電弧判斷條件": "Arc detection criteria", "電弧設定": "Arc settings",
    "電弧計數溢位": "Arc counter overflow", "Imax 門檻": "Imax threshold", "Ix 門檻": "Ix threshold", "Ux 門檻": "Ux threshold",
    "硬電弧中斷時間": "Hard arc interrupt time", "微電弧中斷時間": "Micro arc interrupt time", "dU 門檻": "dU threshold",
    "輸出電壓下降比例（Sag）": "Output voltage drop ratio (Sag)", "Imax 偏移": "Imax offset", "Arc Burst 連續次數": "Arc Burst consecutive count",
    "電弧率門檻": "Arc rate threshold", "Arc Burst 中斷時間": "Arc Burst interrupt time", "Arc Burst 導通時間下限": "Arc Burst minimum on-time",
    "電弧同步": "Arc synchronization", "點火啟用": "Ignition enabled", "停用點火電壓判斷": "Disable ignition voltage check",
    "點火電流門檻": "Ignition current threshold", "點火電壓門檻": "Ignition voltage threshold", "啟用的介面": "Enabled interfaces",
    "開機控制來源": "Control source at power-up", "目前控制來源": "Current control source", "RS232 鮑率": "RS232 baud rate",
    "RS485 位址": "RS485 address", "通訊逾時": "Communication timeout", "並聯模式": "Parallel mode", "並聯從機數": "Number of parallel slaves",
    "並聯 Psetmax": "Parallel Psetmax", "Iset 上限": "Iset limit", "次": "times",
    "EEPROM 錯誤": "EEPROM error", "FPGA 錯誤": "FPGA error", "24 V 供電異常": "24 V supply fault", "過溫": "Overtemperature",
    "市電電壓下降": "Mains voltage drop", "高壓電源通訊異常": "HV supply communication fault", "介面通訊異常": "Interface communication fault",
    "控制來源中斷（ActSourceFail，超過 4–5 秒沒收到命令）": "Control source lost (ActSourceFail, no command for more than 4–5 s)",
    "U500 異常": "U500 fault", "U800 異常": "U800 fault", "Inverter 異常": "Inverter fault", "無負載": "No load", "短路": "Short circuit",
    "電弧密度過高": "Arc density too high", "冷卻水進水溫度異常": "Cooling water inlet temperature abnormal", "PFC 緊急停止": "PFC emergency stop",
    "反向過電流": "Reverse overcurrent", "輸出緊急停止": "Output emergency stop", "風扇關閉過久": "Fan off for too long", "電弧過多": "Too many arcs",
    "電源溫度警告": "Power supply temperature warning", "冷卻水流量過低": "Cooling water flow too low", "冷卻水流向錯誤": "Cooling water flow direction wrong",
    "介面通訊中斷": "Interface communication lost", "目前控制來源通訊中斷": "Communication with the current control source lost",
    "設定值與實際值偏差過大": "Deviation between setpoint and actual value too large", "無法起弧（No Plasma）": "Cannot ignite (No Plasma)",
    "冷卻水進水溫度過低": "Cooling water inlet temperature too low",
  });
  PATTERNS.unshift(
    P(/^共 (\d+) 筆$/, "$1 experiments"),
    P(/^符合 (\d+) 筆$/, "$1 match"),
    P(/^(.+)  ch(\d+)$/, "$1  ch$2"),
    P(/^(\S+) 次$/, "$1 times"),
    P(/^bit mask：(.+)。寫入方式未在實機驗證$/, "bit mask: $1. Write method not verified on real hardware"),
    P(/^雙極時為 100·a\/\(a\+b\)，0\.1 % 步進$/, "For bipolar: 100·a/(a+b), in 0.1 % steps"),
    P(/^手冊範圍 0–20; 2026-11-02 面板接受 >20 並實測到 45 \(Off 為每個脈衝的 %，非週期 %\); 46–98 未驗證; 10\/22 讀到 80$/,
      "Manual range 0–20; on 2026-11-02 the panel accepted values above 20 and was tested up to 45 (Off is a % of each pulse, not of the period); 46–98 not verified; 80 was read on 10/22"),
    P(/^手冊範圍 0–20；2026-11-02 面板接受 >20 並實測到 45（Off 為每個脈衝的 %，非週期 %）；46–98 未驗證；10\/22 讀到 80$/,
      "Manual range 0–20; on 2026-11-02 the panel accepted values above 20 and was tested up to 45 (Off is a % of each pulse, not of the period); 46–98 not verified; 80 was read on 10/22"),
    P(/^(溫度|距離|光譜儀) (\d+) 筆$/, "$1: $2 rows"),
    P(/^音訊 ([\d.]+) 秒$/, "Audio: $1 s"),
    P(/^結果：(.+)$/, "Result: $1"),
    P(/^實驗時間 ([\d.]+) 秒到$/, "Experiment time of $1 s reached"),
    P(/^使用者按「結束實驗」提前結束（通電 (\S+) 秒／設定 (\S+) 秒）$/, "Ended early by the user (\"End experiment\"): output on for $1 s of the set $2 s"),
    P(/^錄製基線中（電源關閉，(\d+) 秒後開啟輸出）$/, "Recording baseline (power off, output turns on in $1 s)"),
    P(/^· (.+)$/, "· $1"),
    P(/^實驗時間 (.+)$/, "Experiment duration $1"),
    P(/^實驗錄製中 \((.+)\)$/, "Experiment recording ($1)"),
    P(/^輸出中（實驗時間 (\d+) 秒）$/, "Output on (experiment duration $1 s)"),
    P(/^已寫入 (.+)$/, "Written: $1"),
    P(/^寫入後讀回的值不一致：(.+)$/, "Value read back after writing differs: $1"),
    P(/^已連線（(.+)），輸出關閉$/, "Connected ($1), output off"),
    P(/^電源連線中斷：(.+)$/, "Power supply connection lost: $1"),
    P(/^電源(警報|警告) (\d+)：(.+)（原文：(.+)）$/, "Power $1 $2: $3 (original: $4)"),
    P(/^連續 (\d+) 次沒有收到電源回應，輸出已要求關閉$/, "No response from the power supply $1 times in a row; output off requested"),
    P(/^(\d+) 秒內主繼電器沒有接通，已取消$/, "Main relay did not close within $1 s; cancelled"),
    P(/^(\d+) 秒內電源沒有回報輸出開啟，已取消$/, "The supply did not report output on within $1 s; cancelled"),
    P(/^目前狀態「(.+)」不能開啟輸出$/, "Cannot turn output on in the current state \"$1\""),
    P(/^寫入後讀回 (.+)，跟要寫入的 (.+) 不同$/, "Read back $1 after writing, different from the $2 to be written"),
    P(/^(.+)（ch(\d+)）(.+)$/, "$1 (ch$2) $3"),
    P(/^(.+)：(必須是整數|只能是 0 或 1|不是數字|不是有效數字)$/, "$1: $2"),
    P(/^必須是整數$/, "must be an integer"),
    P(/^只能是 0 或 1$/, "must be 0 or 1"),
    P(/^電源回應 ([0-9A-F]{4}) (.*)$/, "Power supply replied $1 $2"),
    P(/^電源(警報|警告) (\d+)：(.+)$/, "Power $1 $2: $3"),
  );

  // ---------------------------------------------------------------------------------------------
  // 翻譯引擎
  // ---------------------------------------------------------------------------------------------
  const SPLITS = [["\n", "\n"], ["；", "; "], [" · ", " · "], ["、", ", "], ["，", ", "], ["：", ": "], ["+", " + "], ["  |  ", "  |  "]];
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

  function tr(s, depth = 0) {
    if (lang !== "en" || typeof s !== "string" || !CJK.test(s) || depth > 6) return s;
    const m = /^(\s*)([\s\S]*?)(\s*)$/.exec(s);
    const lead = m[1], core = m[2], trail = m[3];
    if (!core) return s;
    let out = core1(core, depth);
    // 已無漢字、只剩全形標點（例如電源回報的「bit0 …、bit1 …」）：換成半形
    if (depth === 0 && !/[\u3400-\u9fff]/.test(out)) {
      out = out.replace(/、|，/g, ", ").replace(/；/g, "; ").replace(/：/g, ": ").replace(/（/g, " (").replace(/）/g, ")").replace(/。/g, ".");
    }
    return out === core ? s : lead + out + trail;
  }
  function core1(core, depth) {
    if (has(EN, core)) return EN[core];
    for (const [re, to] of PATTERNS) {
      const m = re.exec(core);
      if (!m) continue;
      if (typeof to === "function") return to(m);
      return to.replace(/\$(\d)/g, (_, i) => tr(m[i] == null ? "" : m[i], depth + 1));
    }
    for (const [sep, join] of SPLITS) {
      if (!core.includes(sep)) continue;
      const parts = core.split(sep);
      if (parts.length < 2) continue;
      const done = parts.map((p) => tr(p, depth + 1));
      if (done.some((p, i) => p !== parts[i])) return done.join(join);
    }
    return core;
  }

  // ---------------------------------------------------------------------------------------------
  // DOM 套用
  // ---------------------------------------------------------------------------------------------
  const ATTRS = ["title", "placeholder", "aria-label"];
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "TEXTAREA", "CODE"]);
  const skipped = (n) => { for (let e = n.nodeType === 1 ? n : n.parentNode; e && e.nodeType === 1; e = e.parentNode) if (SKIP_TAGS.has(e.tagName) || e.hasAttribute("data-no-i18n")) return true; return false; };

  function fixText(n) {
    const v = n.nodeValue;
    if (!v || !CJK.test(v) || skipped(n)) return;
    const r = tr(v);
    if (r !== v) n.nodeValue = r;
  }
  function fixAttrs(e) {
    for (const a of ATTRS) {
      const v = e.getAttribute && e.getAttribute(a);
      if (v && CJK.test(v)) { const r = tr(v); if (r !== v) e.setAttribute(a, r); }
    }
  }
  function walk(n) {
    if (n.nodeType === 3) { fixText(n); return; }
    if (n.nodeType !== 1 || skipped(n)) return;
    fixAttrs(n);
    for (let c = n.firstChild; c; c = c.nextSibling) walk(c);
  }
  function apply() {
    document.documentElement.lang = "en";
    if (document.title) document.title = tr(document.title);
    walk(document.body);
    new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.type === "characterData") fixText(m.target);
        else if (m.type === "attributes") { if (!skipped(m.target)) fixAttrs(m.target); }
        else m.addedNodes.forEach(walk);
      }
    }).observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  }

  // 頁首語言切換：連結帶 ?lang=（保留其他查詢參數）
  function addToggle() {
    const host = document.querySelector(".topbar-meta");
    if (!host) return;
    const u = new URL(location.href);
    u.searchParams.set("lang", lang === "en" ? "zh" : "en");
    const a = document.createElement("a");
    a.className = "chip"; a.id = "lang-toggle"; a.style.textDecoration = "none";
    a.setAttribute("data-no-i18n", ""); a.href = u.pathname + u.search + u.hash;
    a.textContent = lang === "en" ? "中文" : "English";
    a.title = lang === "en" ? "切換成中文" : "Switch to English";
    a.lang = lang === "en" ? "zh-Hant" : "en";
    host.appendChild(a);
  }

  window.I18N = { lang, tr, t: tr };
  addToggle();
  if (lang === "en") apply();
})();
