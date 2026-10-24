# TruPlasma 控制研究（lab-sensor-pc 2026-10-24 回報，原檔在該機 Desktop\truplasma_logger\control_research_20261024.md）

## 結論摘要（1/5）
- 4010：Un 1000 V、In 25 A、Pn 10 kW。
- 6040 每 ≤3 s 一次；4–5 s 沒收到 → alarm 61613 ActSourceFail，輸出關閉。timeout 值在 ch5，現值 3 s。
- 開機順序（手冊 PVDPower 頁）：有 alarm 先 Reset Alarms → 確認 Interlock = 0 → 設 U/I/P/freq/duty → Mains relays ON → 等 Relays ON ack → Power ON。analog 章節寫 relays 與 power 之間第一次啟動要隔約 3 s。
- Uset/Iset/Pset 只能用 6040 設定；其他參數用 61x1 設定，會寫入 EEPROM。
- Control source ch19 = ch20 = 1（RS232）；panel 的 Power 鍵只在 DISPLAY 模式下有效。手冊沒寫怎麼釋放 RS232 控制，推測是 6111 把 ch20 寫成 0x08，未驗證。
- LabVIEW 只有讀取命令，沒有 6040。PVDPower 在 2026-01-13 確定開過一次。推測平常用 PVDPower 開機。
- 待驗證：ch650 Off Time = 80（手冊範圍 1–20）；PlasmaBit 位置在手冊和 PVD dump 之間差 1 位。

## 1a. RS232 protocol（M §9.1 p.67–79）
實體層：背板 DB9，8N1，baud 由 ch28 設定（bit4 = 115200）。本機設備是 slave，不主動送資料。

Command：[0]LEN [1]~LEN [2]DSTH [3]DSTL [4]SRCH [5]SRCL [6]CMDH [7]CMDL [8..]data [LEN-2]CRCH [LEN-1]CRCL
Reply：[0]LEN [1]~LEN [2-5]DST/SRC [6]ACKH [7]ACKL [8]CMDH [9]CMDL [10..]data CRCH CRCL
- ~LEN = LEN XOR FF；DST 預設 FFFF，SRC 預設 0000；回應的 DST/SRC 是 00 00 00 00
- CRC = sum(byte[2]..byte[LEN-3]) & 0xFFFF，big-endian
- 位元組順序：CHN/ACK 是 BE；float 是 LE；int16/int32 是 BE

### 6040 Normal run（LEN 0x17）
8-11 Uset float V（0…Un）；12-15 Iset float A；16-19 Pset float kW；20 Ctrl；21-22 CRC。沒有 scaling。float 推測是 LE。
Ctrl：b0 Mains relays（edge 0→1）；b1 Power ON（edge）；b2 Reset arc counters；b3 RS controls generator（edge）；b4-6 不使用；b7 Reset Alarms

### 6040 回應（LEN 0x2A）
10-13 Uact mean V；14-17 Iact **peak** A；18-21 Pact mean kW；22 Bits0；23 Bits1；24 Bits2；25 Bits3；26-27 Imax cnt；28-29 UxI；30-31 dU；32-33 Usag；34-35 ArcBurst（推測 BE）；36-39 Arc/s float；40-41 CRC
- Bits0：0 Relays ON ack、1 Power ON/INHIBIT、2 Ready、3 RS232/485 control、5 MessageRead、7 PlasmaBit（PVD dump 寫的是 b6 PlasmaBit、b7 Plasma On，跟手冊差 1 位，要驗證）
- Bits1（mode）：0 DC-A、1 DC-B、2 MonoPulse-A、3 MonoPulse-B、4 Full、5 BiPulse、6 Trapez、7 Step
- Bits2：0 Interlock、3 FPGA error、4 Power Off Sequence、6 Warning Active、7 Alarm Active
- Bits3：0 RegU、1 RegI、2 RegP、5 Low Freq Range、7 Arc occ.

### 其他指令（LEN 送出 / 回應）
6101 ID 0A/19（回應 CMD 7701，char[13]）；6111 set byte 0D/0E；6112 read byte 0C/0F；6121 set int 0E/0E；6122 read int 0C/10；6141 set float 10/0E；6142 read float 0C/12；6151 set dint 10/0E；6152 read dint 0C/12；6301 read alarm 0A/可變長度 [CODE + 字串]；6302 read last alarm 0A/可變長度

### ACK
4000 OK；4001 length；4002 checksum；4004 unknown cmd；4006 channel not exist；4010 EEPROM write error；4020 EEPROM disabled by slave mode；4030 EEPROM disabled；4031 above upper limit；4032 below lower limit

### Alarm / Warning
Alarm：61601 EEPROM；61603 FPGA；61605/06 24V；61607 Overtemp；61609 Mains sag；61610 HVPS comm；61611 interface comm；61613 ActSourceFail；61616/17 U500；61618 Inverter；61619-21 U800；61623 No Load；61624 Short Circuit；61625 Arc density；61631 inlet water temp；61694 PFC emergency；61695 Reverse overcurrent；61709 Output emergency；61710 Fans off too long
Warning：61654 too many arcs；61657 Overtemp；61658 Low water flow；61659 Wrong flow dir；61661 lost comm interface；61663 lost comm actual control source；61665 set/act deviation (1 U, 2 I, 3 P)；61666 No Plasma；61670 Low inlet water temp

## 1b. 可寫 channel（M p.75-77）。格式：ch 名稱 範圍 | 出廠 D2020 | 現值 R1022
### Byte（6111/6112）
200 Arc criteria enable bits（0 dU、1 UxI、2 Imax、3 Usag、4 ArcBurst、5 ATG）| 5 | 6
202 Arc counters overflow b0 | 0 | 0
209 Arc config（0 Opposite Pulse、1 Soft Start after Hard Arc）| 1 | 1
252 Uout Sag Factor % 10-50 | 20 | 20
261 Imax Offset % 0-100 | 0 | 0
265 Arc Burst arcs in row 1-100 | 3 | 3
18 Active Interfaces（0 RS232、1 Analog、2 EtherCat、3 Display、5 Profibus）| 171 | 171
19 Initial control source（同上 bit）| 1 | 1
20 Actual control source | 1 | 1
28 RS speed | 16 | 16
1006 PB Float Standard 0-1 | 1 | 1
1007 Profibus ID 0-127 | 120 | 120
33 Pulse Mode（bit mask：0 DC-A、1 DC-B、2 MonoPulse-A、3 MonoPulse-B、4 Full、5 BiPulse、6 Trapez、7 Step）| 1 | 2。推測寫入時也要用 mask
120 Parallel cfg | 0 | 0
140 Low Frequency Enable b0 | 0 | 1
451 Ignition Enable b0 | 1 | 0
454 Ignition U criterion Disable b0 | 0 | 0
650 Off Time % 1-20 | 20 | 80（超出範圍）
652-655 Step Up/Down Time A/B % 0-30 | 10 | 10
803 Power Equalizer Enable b0 | 1 | 0
1200 Parallel Slaves Number 1-4 | 1 | 1
1512 Arc Synchronisation（0 dU、1 UxI、2 Imax、3 Usag）| 0 | 0
### Word（6121/6122）
270 Arc Rate Thld arc/s 0-40000 | 0 | 0
275 Arc Burst Break Time us 25-10000 | 1000 | 1000
276 Arc Burst On-Time Below us 1-1000 | 120 | 120
3 RS485 Address | 0 | 0
5 Communication Timeout s 0-65 | 3 | 3
21000-21002 PB scale | 10000
1100 Low Frequency set point Hz 1-1000 | 50 | 500
### Float（6141/6142）
205 Imax Thld A 2.5-32.5 | 32.5 | 32.5
207 Ix Thld A 2.5-25 | 20 | 25
208 Ux Thld V 0-800 | 125 | 100
250 Hard Arc Break Time us 10-2000 | 100 | 100
251 Micro Arc Break Time us 10-1000 | 50 | 50
256 dU Thld % 10-100 | 40 | 80
450 Ign I Thld A 0-2.5 | 2.5 | 2.5
453 Ign U Thld V 200-1000 | 800 | 800
51130 Frequency set point kHz 0.1-100 | 1.0 | 1.0
51131 Duty set point % 0-99（panel 1-99）| 80 | 20
65461 Parallel Psetmax kW 10-300 | 100 | 100
唯讀：233/234 arc rate、282/283、650-653 act step time、51140/51141、65455、906/907、920-947、966-984、DWord 100/900/901

## 1c. 面板 ↔ RS232（M §8 p.58-66）
- U/I/P set/act → 6040；Mode → ch33 / Bits1；Power ON/OFF → Ctrl b0+b1；Reset Alarms → b7；Reset Counters → b2
- Arc Mgmt → 200/209/202/256/252/251/205/207/208/250/261/275/265/276
- Communication → 18/19/20/3/28/5/1007/21000-2/1006
- Configuration → 51130/51131/140/1100/650/803（act 51140/51141）
- Ignition → 451/454/450/453；Parallel → 120/1200/65461/65455；Synchro → 1512
- Alarms → 6301/6302、Bits2 b6/b7；Information → 6101、100/900/901/906/907
- 面板顯示設定、Key number 手冊沒寫 RS232 對應

## 1d. 開機順序與控制權
手冊明確寫到的部分：
1. PVDPower Normal run（M p.148）：Alarm Active → 查原因後 Reset Alarms；Interlock 亮 → 檢查上蓋開關／analog J3 pin12 是否接 +24V；設定 U/I/P/freq/duty；Mains relays ON，等 "Relays ON" bit 亮起，再 Power ON。
2. Analog（M p.47）：先要有 Ready，再設 RelaysOn，約 3 s 後設 PowerOn（只有第一次需要）。電源依輸入的變化（slope）動作，不看狀態。
3. Ctrl b0/b1/b3 是邊緣觸發；b2 送一次就生效；b7 Reset Alarms。
4. 每 ≤3 s 必須送一次指令；4–5 s 沒收到 → 61613。
5. 面板的 Power 只在 DISPLAY 模式下有效。PVDPower 的 "Present control source" 可以暫時切換控制源。
6. Ignition：I < Ign I Thld 且 U > Ign U Thld 時，每 20 ms 發一次脈衝；10 次失敗 → 61666 NoPlasma，暫停 2 s 後重試。

推測（手冊沒寫）：
- 建議序列：0x00（有 alarm 先送 0x80 再回 0x00）→ 0x08（取得 RS 控制，確認 Ready=1、Interlock=0）→ 0x09（relays，等 Bits0.b0=1，第一次至少 3 s）→ 0x0B（Power，確認 Bits0.b1=1）→ 運轉中每 ≤1 s 重送 0x0B 和 setpoint 當心跳 → 關機送 0x09，再送 0x08 或 0x00。
- 每一幀都要持續帶 b0/b1=1，送 0 就會關。
- 釋放 RS 控制：手冊沒有專用指令，可能的做法是 6111 把 ch20 寫成 0x08（未驗證）。

## 2. LabVIEW
seannnnnn1017/Labview，commit 期間 2023-11 ~ 2024-01，是在別台電腦開發的。只有 6142/6112 讀取，沒有 6040，也沒有 61x1 寫入，推斷不出平常用的 U/I/P 值。
