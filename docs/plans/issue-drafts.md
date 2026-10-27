# 待開 issue 草稿（尚未發佈，發佈前需使用者確認）

來源：`docs/plans/issue-3-data-browser.md` §0.4、§7。

---

## A. 多聲道 WAV 產生的頻譜失真
- 標籤：bug
- 內容：`app/chunked_spectrogram.py:70-72` 把 WAV 的 frames 當成單聲道 int16 序列直接丟進 `mlab.specgram`。當 `audio.channels > 1` 時，樣本是交錯的（L R L R…），算出的頻譜不正確。
- 重現：以 2 聲道裝置錄音，再比較只取 ch0 重算的頻譜與 CSV。
- 建議：先決定多聲道要取 ch0、混成單聲道，還是逐聲道各算（對應 #3 Q8）。
- 相關：#3

## B. 頻譜 CSV 以 `%.6f` 輸出線性 PSD，小值被截成 0
- 標籤：bug
- 內容：`DB_like` 欄實際上是線性 PSD，不是 dB。`np.savetxt(fmt="%.6f")`（`chunked_spectrogram.py:303`）會把 < 5e-7 的值寫成 0。實測舊資料有 3–10% 的值是 0。欄名 `DB_like` 也容易造成誤解。
- 建議：與「頻譜儲存格式」issue（C）一起決定，例如改用 `%.6e` 或改存 dB。
- 相關：#3、C

## C. 討論：頻譜資料的儲存格式與內容
- 標籤：discussion
- 內容：頻譜 CSV 每小時約 298 MB，比 WAV（159 MB）還大，而且有冗餘（Power = DB_like²，SNR 每個頻率是常數）。頻譜可以從 WAV 重算，但無法反推回 WAV。需要決定：要存全頻譜還是特徵、用 CSV 還是二進位、下游分析需要什麼。
- 待辦：確認下游使用者與軟體（Excel、Origin、MATLAB、Python？）。
- 相關：#1、#2、#3（Q4、Q5）

## D. 溫度與距離 CSV 的時間欄位不一致
- 標籤：bug
- 內容：
  - 溫度的 `Timestamp` 是 ISO 字串，距離的 `Timestamp` 是 epoch float（`web_monitor.py:1086,1278`）。
  - 溫度的 `Elapsed(s)` 以實驗開始為 0，距離的 `Elapsed(s)` 以第一筆有效讀值為 0（`web_monitor.py:1239`）。
  - 兩份資料直接對齊時會產生偏移。
- 建議：統一成同一種格式與同一個零點。注意要相容已存的舊檔（讀取端仍需換算）。
- 相關：#3

## E. `spectrogram_<id>_metadata.txt` 未指定編碼
- 標籤：bug（小）
- 內容：寫檔時沒有指定 `encoding`，Windows 上會用 cp950，macOS 上用 UTF-8，跨平台讀取可能出現亂碼。
- 建議：統一使用 `utf-8`。
- 相關：#3

## F. PEO 實驗條件標註與標籤
- 標籤：enhancement
- 內容：目前 `experiment_<id>.json` 沒有 PEO 實驗條件，例如電壓或電流模式、電解液組成、試片材料與編號、處理時間、溫控。疊圖比較時無法辨識各筆實驗。希望能在開始實驗時，或事後在歷史頁補填，存成獨立的 `notes.json`，不修改原始 json。
- **待辦：先問材料系學長平常怎麼記錄 PEO 實驗條件（欄位、單位、命名習慣），再定 schema。**
- 相關：#3（Q9）
