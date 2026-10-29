# TruPlasma 電源控制併入 Sensor-web-page（2026-10-24 起）

使用者決定（2026-10-24）：
- 控制範圍：手冊上能用 RS232 控制的項目，包含面板上能控制的功能。
- 控制按鈕只限本機（lab-sensor-pc 本機瀏覽器）；其他電腦只能看。
- 軟體上限用額定值 1000 V / 25 A / 10 kW。
- 現行啟動 PEO 的程式不確定（LabVIEW Plasma_Command_Test 或 PVDPower），要先查。

## 待辦
- [x] 1. lab-sensor-pc 查證：hostname、LabVIEW 專案送的命令與參數、PVDPower 有沒有在用
- [x] 2. 從手冊整理 RS232 命令表（6040 格式、Ctrl bits、狀態位元、警報碼）和面板能控制的項目
- [x] 3. 寫驅動 `sensors/truplasma/`：協定、6040 心跳（<3 s）、讀值、寫入白名單與範圍檢查
- [x] 4. 模擬器（--simulate 也能跑）
- [x] 5. 後端整合 web_monitor.py：狀態、記錄 CSV、控制 API（只限 127.0.0.1）
- [x] 6. 前端：電源卡、即時 U/I/P、控制面板、確認對話框、緊急關閉
- [x] 7. 安全：網頁或程式斷線 → 停止心跳 → 電源 4–5 s 內自動關閉；啟動時一律 Power OFF
- [x] 8. Mac 上用模擬模式驗證
- [ ] 9. 實機測試（要有人在現場、先不接負載）——需使用者安排
- [x] 10. README 更新

## 進度紀錄
- 2026-10-24：1、2 完成（研究筆記 truplasma-protocol-research.md）。3、4、7 完成（sensors/truplasma_package，tests/test_truplasma.py 11 passed）。5、6、10 已派 subagent 實作。
- 決定：通訊、Profibus、並聯參數只顯示不寫入；參數寫入只在輸出關閉時允許；緊急關閉任何連線者都能按。
- 2026-10-24：5、6、10 完成（subagent 實作、主對話審查）。8 完成：模擬模式下 pytest 11 passed；curl 測過所有 API 與 403；無頭 Chrome 走過連線 → 設定 → 開啟（確認對話框）→ 讀參數（輸出中寫入鈕停用）→ 緊急關閉；修了量測表欄位錯位 bug；伺服器收到 SIGTERM 時會先關閉輸出。
- 剩下 9：實機測試，要使用者在現場。
- 2026-10-30：測試 A（長時間穩定測試）腳本 scripts/soak_test.py 由 Codex（GPT-6.1 Terra）實作、主對話審查兩輪。Mac 模擬模式 60 分鐘（連線電源＋錄製）通過：RSS 斜率 1.6 MB/h、執行緒 12–15 不成長、電源 7117 次輪詢 0 失敗、最長未通訊 0.51 s。結果 logs/soak_20261030_011536_396388/。待辦：lab-sensor-pc 實機（只連線、不開輸出）再跑一次，需使用者同意。
