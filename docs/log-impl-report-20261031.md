# Log 與警告改善 實作報告（2026-10-31）

依據：`docs/log-improvement-spec.md`（R1–R4、驗收 A–C）。全程只用模擬器：沒有連真電源、沒有開 COM8／COM10、沒有載入 LKIF2 DLL、沒有對任何真實儀器送寫入命令。沒有 push。沒有改 `--host`／網路綁定、遠端緊急關閉、CSV 欄位。沒有處理 Dashboard A/B 圖表。

## 1. Commit 與證據

| R | Commit | 改了什麼 | 測試證據（`tests/test_log_improvements.py`） |
|---|---|---|---|
| R1 保留原始資訊 | `743e06a` | RC 碼查不到時寫「未知碼 0x2003」，不再是 None（`LKIFError` 帶數值）；電源警報／警告的 log 與通知保留電源原文（含 Mod 與溫度值），翻譯並列；experiment JSON 新增 `device_codes`（代碼、數值、翻譯、原文、次數、首末時間），包含開始前就存在的電源訊息 | `test_rc_text_never_none`、`test_alarm_message_keeps_raw_next_to_translation`、`test_controller_reports_raw_alarm_text`、`test_rangefinder_rc_codes_in_json_and_log`、`test_psu_raw_message_in_json` |
| R2 暫時失誤與真故障 | `afa8bf1` | 共用 `FailureTracker`：連續 ≥10 次且 ≥5 秒才升級（WARN／ERROR＋通知＋JSON `events`，traceback 只在升級時附一次）；之前只在恢復時寫一行 INFO（次數、秒數、代碼序列，例如 `RC=0x2003→RC=0x2008`），不通知；每 60 秒最多逐筆寫 5 行，其餘併成一行；停止時寫「停止時仍在失敗」與「短暫失誤彙總」（次數、最長持續、錯誤碼），並寫進 JSON `failure_summary`；電源 heartbeat 無回應也走同一機制 | `test_tracker_needs_count_and_duration`、`test_tracker_transient_rate_limit`、`test_transient_rc_blip_is_one_info_line`、`test_persistent_alarm_escalates_once_then_recovers`、`test_failing_at_stop_is_reported`、`test_confirmed_failure_counts_from_its_real_start` |
| R3 去重與等級一致 | `55588d1` | 電源事件仍是兩行（系統＋電源），但等級相同（controller 的 error 改記 ERROR）；Test version 家族（61704／61711／61714）只記 INFO、不通知、不顯示紅色，附原文、手冊原文（目前只有 61714）與代碼，不稱故障、不說韌體是測試版；61657 和 61711 交替出現時不再把 61657 重記一次；預檢「先預檢、確認後開始」的第二次結果相同就不再寫；預檢失敗的原始例外併進該感測器那一行，刪掉重複的「預檢警告」 | `test_test_version_family_is_quiet_and_logged_once`、`test_real_warning_not_relogged_when_alternating_with_test_version`、`test_psu_event_lines_have_same_level`、`test_preflight_logged_once_and_failure_in_one_line` |
| R4 網頁文字 | `f518ca1` | 狀態卡說明改成「短現象：原因與建議」：短現象常駐，原因與建議收在可展開區塊（重繪時保留展開）；ALARM 寫「HEAD B 收不到反射光（ALARM）：檢查距離、角度、表面反光，或換 HEAD」；61657 寫「電源溫度警告（原文：…）」；升級通知只放短現象並指向狀態卡；初始化失敗的通知說明缺哪一種資料 | `test_alarm_text_names_head_and_never_says_disconnect`、`test_usb_wording_only_for_communication_rc`、`test_usb_disconnect_card_and_toast`、`test_61657_wording`、`test_card_detail_has_no_sensor_name_prefix` |
| 驗收修正（R1–R4） | `bb60a12` | 見第 3 節：驗收時發現的問題，修在同一個 commit | `test_psu_raw_change_and_clear_are_logged_quietly`、`test_alarm_output_drop_has_alarm_level`，以及更新後的 R4 測試 |

驗收 A：
- 原本的 25 個測試加上新的 22 個，共 **47 個全部通過**（`uv run --with pytest pytest tests`）。
- `node --check app/static/monitor.js` 通過。
- 用完整模擬模式啟動伺服器，可以連上模擬電源、開輸出、開始、停止、斷線。log 沒有任何 ERROR。

## 2. 驗收 B：情境 × 是否達標

做法：
1. 一個獨立、沒有記憶的 subagent 只讀文件、不看程式碼，列出 40 個情境。
2. 我用兩種方式產生情境：
   - **(a) 真伺服器**：`--simulate all --simulate-faults`，用 HTTP 操作。先開模擬電源輸出，再跑 100 秒。
   - **(b) 注入 harness**：程式內建模擬做不出 RC 碼、61657／61711／61714、USB 斷線、門檻邊界，所以用 harness 注入。harness 跑的是真的 `MonitorService`，只把模擬器換成可以照腳本走的版本。
3. 第二個獨立 subagent 依照 R1–R4 逐情境判定。
4. 依判定修正後，重跑 (b) 的全部情境，並逐條複查。

判定欄的符號：✅ 達標、△ 部分達標、—— 不適用。

| # | 情境 | R1 原始資訊 | R2 暫時／真故障 | R3 去重／等級 | R4 文字 | 通知 |
|---|---|---|---|---|---|---|
| S01 | 測距儀 ALARM 持續 8 秒後恢復 | ✅ | ✅ 5.1 秒、47 次時升級 WARN，恢復時寫一行 | ✅ | ✅ HEAD B 文字，沒有「斷線」 | ✅ 異常 1 則、恢復 1 則 |
| S02 | ALARM 間歇（0.6 秒 ALARM／1 秒 VALID，共 12 次） | ✅ | ✅ 只有 INFO，超過的併成一行，停止時有彙總 | —— | ✅ | ✅ 0 則 |
| S03 | 0x2003→0x2008 每次 0.3 秒，共 8 次，電源輸出中 | ✅ 數值、未知碼 | ✅ 每次一行 INFO，沒有 traceback | ✅ 電源兩行同等級 | ✅ 修正後不再寫「USB 可能中斷」 | △ 電源開關輸出每一步各跳一則通知（見第 4 節） |
| S04 | USB 真斷線（用持續 RC=0x2001 代替） | ✅ | ✅ ERROR，traceback 一次 | —— | ✅ 「通訊層錯誤 RC=0x2001：USB 可能中斷」 | ✅ |
| S05 | 61657 持續，中間夾 61711，原文改變後消失 | ✅ 修正後 4512、4530 都進 log | ✅ 有「已消失」行 | ✅ 61657 只升級一次，61711 是 INFO | ✅ 不寫「過溫」 | ✅ 1 則 |
| S06 | 61657 只出現一次（0.5 秒） | ✅ | ✅ 有「已消失（約 0.5 秒）」 | ✅ | ✅ | ✅ 1 則 |
| S07 | 連線時出現 61714（Mod=0、Mod=1）、61711、61704 | ✅ 修正後 Mod=1 也有記 | —— | △ 61704／61711 沒有手冊原文 | ✅ INFO、不稱故障 | ✅ 0 則 |
| S08 | 電源輸出中，ALARM、溫度無回應、61657 同時發生 | ✅ | ✅ 距離 46 次／5.0 秒，溫度 10 次／9.6 秒 | ✅ | ✅ | ✅ 每個來源 1 則 |
| S09 | 停止時仍在失敗（RC=0x2008） | ✅ | ✅ 停止時寫 WARN 一行 | —— | ✅ 修正後寫「意思待查」 | ✅ |
| S10 | ALARM 開始 1 秒時停止 | ✅ | ✅ 停止那行是 INFO，沒有通知 | —— | ✅ | ✅ |
| S11 | 剛好低於門檻：4.6 秒 ALARM；溫度連續 9 次 | ✅ | ✅ 都只寫 INFO | —— | ✅ | ✅ 0 則 |
| S12 | 未知 RC=0x1005：一次短暫，一次持續 | ✅ | ✅ | —— | ✅ 修正後不再猜「不是 USB」 | ✅ |
| S13 | VALID／ALARM 每筆交替 20 秒 | ✅ | △ 依規格不會升級，但約 45% 的讀值沒有通知 | —— | ✅ | △ 見第 4 節 |
| S14 | 預檢時 OpenDevice RC=0x2000，接著開始 | ✅ RC 原文 | —— | △ 預檢只記一次；但開始時又寫一行「未啟用」（刻意保留，見下方說明） | ✅ 修正後摘要寫「通過，但停用 距離」 | —— |
| SRV | 真伺服器，內建故障模擬，100 秒，電源輸出中 | △ 模擬器把中文對照表編成 ASCII，原文顯示 `??????`（只是模擬器的問題） | △ 修正前音訊全 0 寫成「1 次、0.0 秒」；已修正並有單元測試，但真伺服器沒有重跑這一項 | △ 61625 警報造成兩則通知（輸出關閉、警報碼），修正後兩則都是 ERROR | ✅ | △ 同 S03 |

S14 那一行「未啟用」刻意保留：實驗 log 從開始監測才開始寫，這一行是實驗 log 裡唯一的停用紀錄。

全部情境：
- 沒有出現「斷線」、ALARM 被寫成「USB 中斷」、「過溫／過熱」、「測試版」、RC「None」。
- 預檢在網頁「先預檢、確認後開始」兩次呼叫下只記一次。

產物在 scratchpad 的 `accept\` 資料夾：
- 腳本：`scenarios.py`、`sim_server.py`、`drive_server.py`
- 第一輪結果：`inject\`、`server_run\`
- 修正後重跑：`v2\`
- 判定原文：`judge_matrix.md`

這些產物沒有 commit。

## 3. 驗收中發現並已修正的問題（`bb60a12`）

1. 61657 回來時原文已經變了（4512→4530），新原文只進 JSON，沒進 log。修正：
   - 同一代碼的原文改變時記 INFO（第一次立刻記，之後每分鐘最多一次）。
   - Test version 新出現的原文（例如 Mod=1）也記。
   - 訊息消失時記「已消失（持續時間、最後原文）」。
2. 0x2003、0x2008 這些未知碼被寫成「USB 可能中斷」。修正：
   - 「USB 可能中斷」只用在確定的通訊類錯誤 0x2000、0x2001。
   - 未知碼寫「意思待查 KEYENCE LKIF2.h」。
   - 刪掉「不是 USB 中斷」這種猜測，也刪掉升級訊息裡的「短暫出現後可不處理」。
3. 電源的通知和狀態欄位放反了：通知很長，狀態欄位很短。修正：
   - 通知只放短現象和原文。
   - 原因與建議放在 controller 那一行 log 和電源狀態欄位。
4. 電源警報導致的「輸出已關閉」是 WARN，警報本身是 ERROR。修正：都改成 ERROR。
5. 音訊全 0 升級時寫成「1 次、0.0 秒」。修正：改從靜音開始的時間計算。
6. 預檢摘要在有感測器被停用時仍寫「通過」。修正：改成「通過，但停用 X」，等級 WARN。
7. JSON 的測距儀 FloatResult 沒有寫意思。修正：補上意思。

## 4. 未達標或未完成

- **電源開關輸出的每一步都跳通知**（S03、SRV）：開輸出 3 則、關輸出 2 則。這是 review 的 P7，不在 R1–R4 裡，所以沒有改。
- **每筆交替的 ALARM 不會升級**（S13）：規格的升級條件是「連續」失敗，所以一半以上讀值失敗也不會通知。只會有 INFO 行和停止時的彙總。這是規格本身的缺口。
- **61704、61711 沒有手冊原文**：log 寫「待查手冊」，JSON 的 `manual` 是 null。我手上沒有手冊，沒有編造。
- **電源警報一次跳兩則通知**：「輸出已關閉」和警報碼各一則，等級已經一致。要合併的話得改 controller 的安全流程順序，所以沒有改。
- **測距儀升級那行的標題只寫最後一種代碼**：分布有完整列出（例如 WAITING 37、ALARM 10）。
- **狀態卡的配色**：原本的設計把 warn 也畫成紅色。log 和通知是 WARN 時卡片仍是紅色，這次沒有改配色。
- **沒做的部分**：review 建議的「61657 持續時每 60 秒補一行」不在規格裡，沒有做。只做了「原文改變」和「已消失」兩種行。
- **只有模擬，無法驗證的項目**：
  - 真正拔 USB 時會回傳哪個 RC（S04 是用 0x2001 代替）
  - 0x2003、0x2008、0x1005 的真實意思
  - 真的 61657 原文和電源實際用的文字編碼
  - 狀態卡展開的實際外觀（只檢查資料和 JS 語法，沒有截圖）
  - CSV 沒有和 10-30 的檔案逐欄比對；欄位定義沒有改，但 Status 內容有變，例如 `(None)` 變成 `(未知碼 0x2003)`
- **`OUT 0 = HEAD B`**：根據是 10-30 那次的實測（`RANGEFINDER_HEAD = "B"`），沒有正式確認。

## 5. 需要你決定的事

1. **交替失敗要不要另外設升級規則**（S13），例如 10 秒內失敗超過 30% 就通知。
2. **電源開關輸出的步驟要不要只在失敗或取消時通知**（P7），成功只記 log。
3. **音訊全 0 滿 3 秒就升級**（沿用原本的 3 秒偵測），比規格的 5 秒早。要改成等滿 5 秒，還是當作規格例外？
4. **補 61704、61711 的手冊原文**；也請確認 `OUT 0 = HEAD B`，以及 0x2001 以外還有哪些碼屬於通訊層錯誤。
5. **commit 的數量和署名**：驗收修正放在第 5 個 commit `bb60a12`，所以不是嚴格的「一個 R 一個 commit」。另外 commit 署名依你的指示寫 Claude Sonnet 5.5，但實際執行的是 Opus 5.5；spec 寫「Codex 實作、Claude 審查」，這次依你的指示由我實作。要不要合併 commit、改署名？
6. **harness 與情境腳本要不要 commit 進 `scripts/`**，方便之後重跑。

改之前已經把三個沒有進 git 的 docs 備份到 scratchpad 的 `backup_untracked\`，而且都沒有修改。
