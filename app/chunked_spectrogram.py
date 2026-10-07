"""從整段 WAV 分塊產生頻譜 CSV，輸出格式與 signal_package.save_spectrogram_to_csv 完全相同。

原流程（main_csv.py）：plot_spectrogram(history) → Axes.specgram → mlab.specgram(NFFT=256, noverlap=128,
其餘參數預設：detrend_none、Hanning window、onesided、scale_by_freq=True、mode='psd') → 線性 PSD，
再由 save_spectrogram_to_csv 以 np.interp 插值成 0.05 s 間隔、加上 Power = x² 與每個頻率一個 SNR 值。

這裡做的事完全相同，只是不把整段音訊載入記憶體：
1. 分塊讀 WAV，每塊用 mlab.specgram 算出一段連續的時間欄（每個視窗各自 detrend/加窗，分塊結果與整段相同）。
2. 每塊算完就做 0.05 s 插值（np.interp 只需要相鄰兩欄，所以前一塊的最後一欄會帶到下一塊），
   結果寫進 np.memmap 暫存檔；同時累加每個頻率的 max|x| 與 Σ|x|，供 SNR 使用。
3. 再分塊讀 memmap，組出與原版相同的欄位順序，用 np.savetxt(fmt="%.6f") 寫出 CSV。

指令列用法（補產生／重新產生）：
    uv run python app/chunked_spectrogram.py Sensor_Data/EXP_YYYYmmdd_HHMMSS   （在專案根目錄執行）
"""

import datetime
import os
import sys
import time
import wave

import numpy as np
from matplotlib import mlab

NFFT = 256
NOVERLAP = 128
HOP = NFFT - NOVERLAP
TIME_INTERVAL = 0.05  # 與 save_spectrogram_to_csv 相同


def wav_params(path):
    with wave.open(path, "rb") as w:
        return {"nchannels": w.getnchannels(), "sampwidth": w.getsampwidth(),
                "framerate": w.getframerate(), "nframes": w.getnframes()}


def total_samples(path):
    """原程式把 AudioRecorder 回傳的（可能交錯的）int16 樣本直接當成一維訊號，這裡沿用。"""
    p = wav_params(path)
    return p["nframes"] * p["nchannels"]


def spec_bins(n_samples, fs):
    """與 mlab._spectral_helper 相同的時間軸算法。"""
    return np.arange(NFFT / 2, n_samples - NFFT / 2 + 1, NFFT - NOVERLAP) / fs


def n_spec_columns(n_samples):
    return 0 if n_samples < NFFT else (n_samples - NFFT) // HOP + 1


def iter_spec_blocks(path, fs, cols_per_block=4096):
    """依序回傳 (起始欄索引, spec 區塊 (n_freq, n), freqs)。與對整段做 mlab.specgram 逐欄相同。"""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("只支援 16-bit WAV")
        nch = w.getnchannels()
        n_cols = n_spec_columns(w.getnframes() * nch)
        carry = np.empty(0, dtype=np.int16)
        col = 0
        while col < n_cols:
            nb = min(cols_per_block, n_cols - col)
            need = (nb - 1) * HOP + NFFT
            while len(carry) < need:
                frames = w.readframes((need - len(carry) + nch - 1) // nch)
                if not frames:
                    break
                carry = np.concatenate((carry, np.frombuffer(frames, dtype="<i2").astype(np.int16)))
            seg = carry[:need]
            spec, freqs, _ = mlab.specgram(seg, NFFT=NFFT, Fs=fs, noverlap=NOVERLAP)
            if spec.shape[1] != nb:
                raise RuntimeError(f"分塊欄數不符：預期 {nb}，得到 {spec.shape[1]}")
            yield col, np.asarray(spec, dtype=np.float64), np.asarray(freqs, dtype=np.float64)
            carry = carry[nb * HOP:]
            col += nb


def bins_slice(c0, c1, fs):
    """spec_bins(...)[c0:c1]，不必建立整條時間軸。與 np.arange(NFFT/2, ..., HOP)/fs 逐值相同。"""
    return (NFFT / 2 + float(HOP) * np.arange(c0, c1, dtype=np.float64)) / fs


class TimePlan:
    """save_spectrogram_to_csv 的「是否插值／新時間軸」判斷，改成不需要整條陣列的版本。

    新時間軸 = np.linspace(bins[0], bins[0] + 0.05*(num-1), num)，numpy 的實作是 arange(num)*step + start
    且最後一點設為 stop，這裡逐段重現同樣的運算，結果逐值相同。
    """

    def __init__(self, n_cols, fs, block=1 << 20):
        self.n_cols, self.fs = n_cols, fs
        b0 = bins_slice(0, 1, fs)[0]
        b_last = bins_slice(n_cols - 1, n_cols, fs)[0]
        self.b0 = b0
        if n_cols == 1:
            self.interpolated, self.num = False, 1
            return
        # 原版：np.unique(np.round(np.diff(bins), 6)) 與 np.mean(np.diff(bins))，這裡分段計算
        uniq = set()
        for c in range(0, n_cols - 1, block):
            seg = bins_slice(c, min(n_cols, c + block + 1), fs)
            uniq.update(np.unique(np.round(np.diff(seg), 6)).tolist())
        current_interval_mean = (b_last - b0) / (n_cols - 1)
        self.interpolated = len(uniq) > 1 or abs(current_interval_mean - TIME_INTERVAL) > 1e-6
        if not self.interpolated:
            self.num = n_cols
            return
        total_time_span = b_last - b0
        if total_time_span < 0:
            raise ValueError("時間軸範圍無效 (結束時間早於開始時間)。")
        num = int(round(total_time_span / TIME_INTERVAL)) + 1
        self.num = max(1, num)
        self.stop = b0 + TIME_INTERVAL * (self.num - 1)
        self.step = (self.stop - b0) / (self.num - 1) if self.num > 1 else 0.0

    def times(self, a, b):
        """新時間軸的第 a..b-1 點。"""
        if not self.interpolated:
            return bins_slice(a, b, self.fs)
        if self.num == 1:
            return np.array([self.b0])[a:b]
        y = np.arange(a, b, dtype=np.float64) * self.step + self.b0
        if a <= self.num - 1 < b:
            y[self.num - 1 - a] = self.stop
        return y

    def count_le(self, x):
        """新時間軸中 <= x 的點數（等同 np.searchsorted(ib, x, side='right')）。"""
        if not self.interpolated:
            j = int(np.floor((x * self.fs - NFFT / 2) / HOP)) + 1
        elif self.num == 1:
            return 1 if self.b0 <= x else 0
        else:
            j = int(np.floor((x - self.b0) / self.step)) + 1
        j = min(max(j, 0), self.num)
        while j < self.num and self.times(j, j + 1)[0] <= x:
            j += 1
        while j > 0 and self.times(j - 1, j)[0] > x:
            j -= 1
        return j


def write_metadata(filename, experiment_id, timestamp, sample_rate, n_times, n_freqs,
                   save_power=True, save_snr=True):
    """與 save_spectrogram_to_csv 的 _metadata.txt 逐字相同。"""
    metadata_filename = os.path.splitext(filename)[0] + "_metadata.txt"
    with open(metadata_filename, "w") as f:
        f.write(f"Experiment ID: {experiment_id}\n")
        f.write(f"Timestamp: {timestamp}\n")
        f.write(f"Sample Rate: {sample_rate} Hz\n")
        f.write(f"NFFT: {NFFT}\n")
        f.write(f"Noverlap: {NOVERLAP}\n")
        f.write(f"Time Interval (after interpolation): {TIME_INTERVAL} seconds\n")
        f.write(f"Number of Time Bins (after interpolation): {n_times}\n")
        f.write(f"Number of Frequency Bins: {n_freqs}\n")
        f.write(f"Data File: {filename}\n")
        f.write("Data Columns: Time(s), then for each frequency (DB-like")
        if save_power:
            f.write(", Power")
        if save_snr:
            f.write(", SNR(dB)")
        f.write(")\n")
    return metadata_filename


def save_spectrogram_csv_from_wav(wav_path, filename, experiment_id, sample_rate=None,
                                  save_power=True, save_snr=True, cols_per_block=4096,
                                  rows_per_block=2048, progress=None):
    """分塊版 save_spectrogram_to_csv。

    回傳 dict：n_times、n_freqs、n_spec_cols、metadata、seconds。
    """
    t_start = time.time()
    params = wav_params(wav_path)
    fs = sample_rate or params["framerate"]
    n_samples = params["nframes"] * params["nchannels"]
    n_cols = n_spec_columns(n_samples)
    if n_cols < 2:
        raise ValueError(f"音訊太短（{n_samples} 個樣本），無法產生頻譜圖")

    if os.path.exists(filename):
        os.remove(filename)
    timestamp = datetime.datetime.now().isoformat()

    plan = TimePlan(n_cols, fs)
    m = plan.num
    if plan.interpolated:
        print(f"原始時間軸平均間隔 {(bins_slice(n_cols - 1, n_cols, fs)[0] - plan.b0) / (n_cols - 1):.6f}s 或非均勻，"
              f"將插值為 {TIME_INTERVAL}s 間隔。")

    n_freq = NFFT // 2 + 1
    row_bytes = n_freq * 8
    tmp_path = os.path.join(os.path.dirname(os.path.abspath(filename)),
                            f".{os.path.basename(filename)}.interp.tmp")
    # 暫存檔先配置好大小；之後每個區塊只開一個小的 np.memmap 視窗，用完立刻 del，
    # 記憶體用量與錄音長度無關，Windows 上也不會因 mmap 未釋放而無法刪除暫存檔。
    with open(tmp_path, "wb") as f:
        f.truncate(m * row_bytes)
    abs_max = np.zeros(n_freq)
    abs_sum = np.zeros(n_freq)
    freqs = None

    def mm_window(offset_row, rows, mode):
        return np.memmap(tmp_path, dtype=np.float64, mode=mode, offset=offset_row * row_bytes,
                         shape=(rows, n_freq))

    try:
        # ---- 第 1 階段：分塊 specgram + 插值 ----
        prev_x = None
        prev_y = None
        j_next = 0
        for c0, spec, fq in iter_spec_blocks(wav_path, fs, cols_per_block):
            freqs = fq
            nb = spec.shape[1]
            c1 = c0 + nb
            bx = bins_slice(c0, c1, fs)
            if prev_x is None:
                xs, ys = bx, spec
            else:
                xs = np.concatenate(([prev_x], bx))
                ys = np.concatenate((prev_y[:, None], spec), axis=1)
            j_end = m if c1 == n_cols else plan.count_le(bx[-1])
            if j_end > j_next:
                tq = plan.times(j_next, j_end)
                block = np.empty((j_end - j_next, n_freq))
                for i in range(n_freq):
                    block[:, i] = np.interp(tq, xs, ys[i, :])
                mm = mm_window(j_next, j_end - j_next, "r+")
                mm[:] = block
                mm.flush()
                del mm
                a_ = np.abs(block)
                np.maximum(abs_max, a_.max(axis=0), out=abs_max)
                abs_sum += a_.sum(axis=0)
                j_next = j_end
            prev_x, prev_y = bx[-1], spec[:, -1].copy()
            if progress:
                progress(0.6 * c1 / n_cols)
        if j_next != m:
            raise RuntimeError(f"插值輸出不完整：{j_next}/{m}")
        if plan.interpolated:
            print(f"時間軸已插值，新時間點數量: {m}。")

        # ---- SNR（每個頻率一個值，與原版公式相同）----
        snr = np.zeros(n_freq)
        if save_snr:
            for i in range(n_freq):
                noise_level = abs_sum[i] / m
                snr[i] = 20 * np.log10(abs_max[i] / noise_level) if noise_level > 1e-9 else 0

        # ---- 第 2 階段：分塊寫出 CSV ----
        header_parts = ["Time(s)"]
        for freq_val in freqs:
            header_parts.append(f"{freq_val:.2f}Hz_DB_like")
            if save_power:
                header_parts.append(f"{freq_val:.2f}Hz_Power")
            if save_snr:
                header_parts.append(f"{freq_val:.2f}Hz_SNR(dB)")
        full_header = ",".join(header_parts)
        cols_per_freq = 1 + (1 if save_power else 0) + (1 if save_snr else 0)
        n_out_cols = 1 + n_freq * cols_per_freq

        with open(filename, "wt") as fh:  # 與 np.savetxt(filename) 相同的文字模式（Windows 也一樣換行）
            fh.write(full_header + "\n")
            for a in range(0, m, rows_per_block):
                b = min(m, a + rows_per_block)
                mm = mm_window(a, b - a, "r")
                db = np.array(mm)  # 複製出來，立刻釋放 mmap
                del mm
                merged = np.zeros((b - a, n_out_cols))
                merged[:, 0] = plan.times(a, b)
                merged[:, 1::cols_per_freq] = db
                if save_power:
                    merged[:, 2::cols_per_freq] = np.power(db, 2)
                if save_snr:
                    merged[:, (2 + (1 if save_power else 0))::cols_per_freq] = snr
                np.savetxt(fh, merged, delimiter=",", fmt="%.6f")
                if progress:
                    progress(0.6 + 0.4 * b / m)
    finally:
        for _ in range(5):  # Windows：防毒軟體可能短暫鎖檔
            try:
                os.remove(tmp_path)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                time.sleep(0.2)

    meta = write_metadata(filename, experiment_id, timestamp, fs, m, n_freq, save_power, save_snr)
    print(f"成功將合併的頻譜數據 (DB-like, Power, SNR) 儲存到 {filename}")
    print(f"元數據已儲存到 {meta}")
    result = {"n_times": m, "n_freqs": n_freq, "n_spec_cols": n_cols, "metadata": meta,
              "seconds": time.time() - t_start}
    return result


def _cli(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    target = argv[1]
    if os.path.isdir(target):
        exp_id = os.path.basename(os.path.normpath(target))
        wav_path = os.path.join(target, f"audio_{exp_id}.wav")
        csv_path = os.path.join(target, f"spectrogram_{exp_id}.csv")
    else:
        wav_path = target
        exp_id = os.path.splitext(os.path.basename(target))[0].replace("audio_", "")
        csv_path = os.path.join(os.path.dirname(target), f"spectrogram_{exp_id}.csv")
    r = save_spectrogram_csv_from_wav(wav_path, csv_path, exp_id)
    print(f"完成：{r['n_times']} 列 × {r['n_freqs']} 頻率，耗時 {r['seconds']:.1f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
