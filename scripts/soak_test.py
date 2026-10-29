"""監測伺服器的長時間穩定性；只讀取電源狀態或管理本程式建立的連線。"""
import argparse
import csv
import datetime as dt
import json
import math
import re
import statistics
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psutil

RSS_LIMIT_MB_PER_HOUR = 5
RSS_MIN_SECONDS = 1800
COMM_LIMIT_SECONDS = 2
ACTIVE_PHASES = {'relays', 'powering', 'on', 'stopping'}
NORMAL_REASONS = {'ok', 'idle', 'init', 'disabled', 'stopped'}
ROOT = Path(__file__).resolve().parents[1]


def parse_duration(value):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([hms])\s*', value.lower())
    if not match or float(match[1]) <= 0:
        raise ValueError('時間須為大於零的數字，後接 h、m 或 s')
    return float(match[1]) * {'h': 3600, 'm': 60, 's': 1}[match[2]]


def output_active(phase):
    return phase in ACTIVE_PHASES


def slope(points):
    if len(points) < 2:
        return None
    xs, ys = zip(*points)
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    return (sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator * 3600
            if denominator else None)


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = math.floor(index)
    return values[lower] + (values[math.ceil(index)] - values[lower]) * (index - lower)


def summarize(samples, duration, aborted='', simulated=None, notes=None, psu_samples=None, processes=None):
    def values(key):
        return [s[key] for s in samples if s.get(key) is not None]

    def stats(key):
        v = values(key)
        return {'start': v[0], 'end': v[-1], 'max': max(v)} if v else {'start': None, 'end': None, 'max': None}

    rss_points = [(s['elapsed'], s['rss_mb']) for s in samples if s.get('rss_mb') is not None]
    rss_slope = slope(rss_points)
    # 缺失值不代表連線變化；只計算已知狀態由連線轉為斷線。
    psu_samples = samples if psu_samples is None else psu_samples
    def psu_values(key):
        return [s[key] for s in psu_samples if s.get(key) is not None]
    connected = psu_values('psu_connected')
    disconnects = sum(a is True and b is False for a, b in zip(connected, connected[1:]))
    ages = psu_values('last_ok_age')
    errors = sum(bool(s.get('psu_error')) for s in psu_samples)
    latencies = values('http_latency_ms')
    http_failures = sum(bool(s.get('http_error')) for s in samples) + (sum(bool(s.get('http_error')) for s in psu_samples) if psu_samples is not samples else 0)
    sensors = {}
    for i, s in enumerate(samples):
        for name, state in s.get('sensors', {}).items():
            entry = sensors.setdefault(name, {'abnormal_count': 0, 'abnormal_seconds': 0})
            if state.get('reason') not in NORMAL_REASONS:
                entry['abnormal_count'] += 1
                if i + 1 < len(samples):
                    entry['abnormal_seconds'] += max(0, samples[i + 1]['elapsed'] - s['elapsed'])
    thread_values = values('threads')
    thread_growth = len(thread_values) >= 3 and all(a <= b for a, b in zip(thread_values, thread_values[1:])) and thread_values[-1] > thread_values[0]
    def criterion(value, valid, label=None):
        return {'status': label or ('通過' if valid else '不通過'), 'value': value}
    criteria = {
        'rss': criterion(rss_slope, rss_slope is not None and rss_slope < RSS_LIMIT_MB_PER_HOUR,
                         '時間不足' if duration < RSS_MIN_SECONDS else ('資料不足' if rss_slope is None else None)),
        'threads': criterion(thread_values, not thread_growth, '資料不足' if len(thread_values) < 3 else None),
        'disconnects': criterion(disconnects, disconnects == 0, '資料不足' if not connected or True not in connected else None),
        'communication': criterion(max(ages) if ages else None, bool(ages) and max(ages) < COMM_LIMIT_SECONDS,
                                   '資料不足' if not ages else None),
        'http': criterion(http_failures, http_failures == 0),
    }
    return {'duration_seconds': duration, 'sample_count': len(samples), 'simulated': simulated,
            'abort_reason': aborted, 'notes': notes or [], 'rss': {**stats('rss_mb'), 'slope_mb_per_hour': rss_slope},
            'threads': stats('threads'), 'handles': stats('handles'),
            'psu': {'disconnects': disconnects, 'max_fail_count': max(psu_values('psu_fail_count'), default=None),
                    'error_samples': errors, 'max_last_ok_age': max(ages, default=None),
                    'fail_count_positive_samples': sum((s.get('psu_fail_count') or 0) > 0 for s in psu_samples),
                    'poll_count': len(psu_samples)},
            'sensors': sensors, 'http': {'failures': http_failures, 'p50_ms': percentile(latencies, .5),
                                        'p95_ms': percentile(latencies, .95), 'max_ms': max(latencies, default=None)},
            'processes': processes or [], 'criteria': criteria,
            'verdict': ('不通過' if aborted or any(c['status'] == '不通過' for c in criteria.values())
                        else '未判定' if any(c['status'] != '通過' for c in criteria.values()) else '通過')}


def request(url, path, data=None):
    payload = None if data is None else json.dumps(data).encode('utf-8')
    req = urllib.request.Request(url.rstrip('/') + path, data=payload, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as response:
        body = json.load(response)
    if body.get('ok') is False:
        raise RuntimeError(str(body.get('errors', 'API 回報失敗')))
    return body


def psu_observation(snapshot, now, previous):
    last_ok = snapshot.get('last_ok')
    row = {'psu_connected': snapshot.get('connected'), 'psu_phase': snapshot.get('phase'),
           'psu_fail_count': snapshot.get('fail_count'), 'psu_error': snapshot.get('error'),
           'last_ok_age': max(0, now - last_ok) if last_ok is not None else None}
    changes = []
    if (row['psu_fail_count'] or 0) > 0:
        changes.append(('電源通訊失敗', row['psu_fail_count']))
    if previous:
        for key, label in [('psu_phase', '電源 phase'), ('psu_connected', '電源 connected')]:
            if row[key] != previous[key]:
                changes.append((label, f"{previous[key]} → {row[key]}"))
        if bool(row['psu_error']) != bool(previous['psu_error']):
            changes.append(('電源錯誤變化', row['psu_error'] or '已消失'))
    return row, changes


def find_process(url, pid, notes):
    if pid:
        try:
            return psutil.Process(pid)
        except (psutil.Error, OSError) as exc:
            notes.append(f'無法取得指定 PID：{exc}')
            return None
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        notes.append('非本機 URL，無法自動尋找程序；請指定 --pid')
        return None
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    try:
        matches = [c.pid for c in psutil.net_connections(kind='inet') if c.status == psutil.CONN_LISTEN and c.laddr.port == port and c.pid]
        if matches:
            return psutil.Process(matches[0])
    except (psutil.Error, OSError) as exc:
        notes.append(f'無法依監聽埠尋找程序：{exc}')
    candidates = []
    try:
        all_processes = list(psutil.process_iter(['pid', 'ppid', 'cmdline']))
    except (psutil.Error, OSError) as exc:
        notes.append(f'無法掃描程序：{exc}；可用 --pid 指定')
        return None
    for proc in all_processes:
        try:
            cmd = proc.info.get('cmdline') or []
            if not any(Path(arg).name in ('main.py', 'web_monitor.py') for arg in cmd):
                continue
            ports = [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == '--port']
            ports += [arg.split('=', 1)[1] for arg in cmd if arg.startswith('--port=')]
            if ports and str(port) not in ports:
                continue
            if not ports and port != 5002:
                continue
            candidates.append(proc)
        except (psutil.Error, OSError, ValueError):
            continue
    if candidates:
        ids = {p.pid for p in candidates}
        roots = [p for p in candidates if p.info.get('ppid') not in ids]
        if len(roots) == 1:
            return roots[0]
        notes.append('找到多個可能的伺服器程序；請指定 --pid')
    notes.append('無法取得伺服器 PID，程序指標留空；可用 --pid 指定')
    return None


def process_metrics(proc, notes, processes=None, cache=None):
    result = {'rss_mb': None, 'cpu_percent': None, 'threads': None, 'handles': None}
    if not proc:
        return result
    try:
        tree = [proc] + proc.children(recursive=True)
        if cache is not None:
            tree = [cache.setdefault((p.pid, p.create_time()), p) for p in tree]
    except (psutil.Error, OSError) as exc:
        notes.append(f'無法取得完整程序樹：{exc}')
        return result
    for child in tree:
        if processes is not None:
            try:
                processes[child.pid] = child.cmdline()
            except (psutil.Error, OSError):
                processes.setdefault(child.pid, [])
    for key, fn in [('rss_mb', lambda p: p.memory_info().rss / 1024 ** 2),
                    ('cpu_percent', lambda p: p.cpu_percent()), ('threads', lambda p: p.num_threads()),
                    ('handles', lambda p: p.num_handles() if hasattr(p, 'num_handles') else p.num_fds())]:
        try:
            result[key] = sum(fn(p) for p in tree)
        except (psutil.Error, OSError, AttributeError) as exc:
            note = f'無法取得完整程序樹 {key}：{exc}'
            if note not in notes:
                notes.append(note)
    return result


def write_summary(folder, summary):
    (folder / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    names = {'rss': f'RSS 斜率 < {RSS_LIMIT_MB_PER_HOUR} MB/小時（至少 {RSS_MIN_SECONDS / 60:g} 分鐘）',
             'threads': '執行緒數不持續成長（至少三筆，逐筆不下降且末值較高視為持續成長）',
             'disconnects': 'PSU 斷線 0 次', 'communication': f'最長未通訊 < {COMM_LIMIT_SECONDS} 秒', 'http': 'HTTP 失敗 0 次'}
    lines = ['# 長時間穩定測試摘要', '', f"判定：**{summary['verdict']}**", f"結束原因：{summary['abort_reason'] or '完成'}",
             f"實際時間：{summary['duration_seconds']:.1f} 秒；取樣數：{summary['sample_count']}；模擬：{summary['simulated']}", '', '## 判準']
    for key, label in names.items():
        c = summary['criteria'][key]
        lines.append(f"- {label}：{c['status']}；實際值：{c['value']}")
    for label, key in [('記憶體 MB', 'rss'), ('執行緒數', 'threads'), ('檔案／handle 數', 'handles'),
                       ('電源', 'psu'), ('感測器（非正常取樣次數與估計秒數）', 'sensors'), ('HTTP', 'http')]:
        lines.extend(['', f'## {label}', f"`{json.dumps(summary[key], ensure_ascii=False)}`"])
    lines.extend(['', '## 程序（PID 與命令列）', f"`{json.dumps(summary['processes'], ensure_ascii=False)}`"])
    if summary['notes']:
        lines.extend(['', '## 備註'] + [f'- {n}' for n in summary['notes']])
    (folder / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='長時間穩定測試（不操作電源輸出）')
    parser.add_argument('--url', default='http://127.0.0.1:5002')
    parser.add_argument('--duration', required=True)
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--psu-port', default='COM8')
    parser.add_argument('--connect-psu', action='store_true')
    parser.add_argument('--with-recording', action='store_true')
    parser.add_argument('--pid', type=int)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    try:
        duration = parse_duration(args.duration)
        if not math.isfinite(args.interval) or args.interval <= 0:
            raise ValueError('取樣間隔須大於零')
    except ValueError as exc:
        parser.error(str(exc))
    folder = args.out or ROOT / 'logs' / ('soak_' + dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    folder.mkdir(parents=True, exist_ok=False)
    notes, samples, fast_samples, processes = [], [], [], {}
    stop_poll = threading.Event()
    poll_thread = None
    event_lock = threading.Lock()
    proc = find_process(args.url, args.pid, notes)
    process_cache = {}
    if proc:
        process_metrics(proc, notes, processes, process_cache)
    started = time.monotonic()
    connected_by_us = recording_by_us = False
    simulated = None
    reason = ''
    fields = ['timestamp', 'elapsed', 'rss_mb', 'cpu_percent', 'threads', 'handles', 'psu_connected', 'psu_phase',
              'psu_fail_count', 'psu_error', 'last_ok_age', 'sensors', 'http_latency_ms', 'http_error']
    try:
        with (folder / 'samples.csv').open('w', encoding='utf-8-sig', newline='') as sf, (folder / 'events.csv').open('w', encoding='utf-8-sig', newline='') as ef:
            writer = csv.DictWriter(sf, fieldnames=fields)
            events = csv.writer(ef)
            writer.writeheader(); sf.flush()
            events.writerow(['timestamp', 'elapsed', 'event', 'detail']); ef.flush()
            def event(name, detail):
                with event_lock:
                    events.writerow([dt.datetime.now().isoformat(), round(time.monotonic() - started, 3), name, detail]); ef.flush()
            try:
                psu = request(args.url, '/api/psu/state')
                snapshot = psu['snapshot']
                simulated = psu.get('simulated')
                if output_active(snapshot.get('phase')):
                    raise RuntimeError('中止：偵測到電源輸出')
                if args.connect_psu and snapshot.get('phase') == 'disconnected':
                    request(args.url, '/api/psu/connect', {'port': args.psu_port})
                    connected_by_us = True
                if args.with_recording:
                    state = request(args.url, '/api/state')
                    if state.get('phase') not in ('idle', 'stopped'):
                        raise RuntimeError('已有監測正在執行，無法安全啟動錄製')
                    raw = dict(state.get('params', {}))
                    raw.update({'confirm': True, 'label_metal': 'soak test', 'label_electrolyte': 'soak test',
                                'label_specimen_shape': 'square', 'label_specimen_length': '1',
                                'label_specimen_width': '1', 'label_specimen_thickness': '1'})
                    request(args.url, '/api/start', raw)
                    recording_by_us = True
                def poll_psu():
                    previous_psu = None
                    while not stop_poll.is_set():
                        try:
                            snap = request(args.url, '/api/psu/state')['snapshot']
                            obs, changes = psu_observation(snap, time.time(), previous_psu)
                            fast_samples.append(obs)
                            for name, detail in changes:
                                event(name, detail)
                            previous_psu = obs
                            if output_active(obs['psu_phase']):
                                event('中止', '中止：偵測到電源輸出')
                                stop_poll.set()
                                break
                        except (OSError, ValueError, KeyError, RuntimeError) as exc:
                            event('HTTP 失敗', str(exc))
                            fast_samples.append({'http_error': str(exc)})
                        stop_poll.wait(0.5)
                poll_thread = threading.Thread(target=poll_psu, name='soak-psu-poll')
                poll_thread.start()
                previous = None
                while time.monotonic() - started < duration:
                    tick = time.monotonic()
                    row = {'timestamp': dt.datetime.now().isoformat(), 'elapsed': tick - started, **process_metrics(proc, notes, processes, process_cache),
                           'psu_connected': None, 'psu_phase': None, 'psu_fail_count': None, 'psu_error': None,
                           'last_ok_age': None, 'sensors': {}, 'http_latency_ms': None, 'http_error': ''}
                    t = time.monotonic()
                    try:
                        p = request(args.url, '/api/psu/state')
                        snap = p['snapshot']
                        row.update(psu_connected=snap.get('connected'), psu_phase=snap.get('phase'),
                                   psu_fail_count=snap.get('fail_count'), psu_error=snap.get('error'))
                        if snap.get('last_ok') is not None:
                            row['last_ok_age'] = max(0, time.time() - snap['last_ok'])
                        if output_active(row['psu_phase']):
                            reason = '中止：偵測到電源輸出'
                        else:
                            state = request(args.url, '/api/state')
                            row['sensors'] = {k: {'level': v.get('level'), 'reason': v.get('reason')} for k, v in state.get('sensors', {}).items()}
                    except (OSError, ValueError, KeyError, RuntimeError) as exc:
                        row['http_error'] = str(exc)
                        event('HTTP 失敗', str(exc))
                    row['http_latency_ms'] = (time.monotonic() - t) * 1000
                    if previous:
                        for key, label in [('sensors', '感測器狀態')]:
                            if row[key] != previous[key]: event(label, f"{previous[key]} → {row[key]}")
                    samples.append(row)
                    writer.writerow({**row, 'sensors': json.dumps(row['sensors'], ensure_ascii=False)}); sf.flush()
                    previous = row
                    if reason or stop_poll.is_set():
                        reason = reason or '中止：偵測到電源輸出'
                        break
                    stop_poll.wait(max(0, min(args.interval - (time.monotonic() - tick), duration - (time.monotonic() - started))))
                if stop_poll.is_set() and not reason:
                    reason = '中止：偵測到電源輸出'
            except KeyboardInterrupt:
                reason = '使用者按下 Ctrl+C'
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                reason = str(exc)
                event('中止', reason)
            finally:
                stop_poll.set()
                if poll_thread:
                    poll_thread.join()
                if recording_by_us:
                    try:
                        request(args.url, '/api/stop', {})
                    except (OSError, ValueError, RuntimeError) as exc:
                        notes.append(f'停止錄製失敗：{exc}')
                if connected_by_us:
                    try:
                        phase = request(args.url, '/api/psu/state')['snapshot']['phase']
                        if output_active(phase) or phase not in ('idle', 'disconnected'):
                            notes.append(f'安全起見未斷開電源：phase={phase}')
                        elif phase == 'idle':
                            request(args.url, '/api/psu/disconnect', {})
                    except (OSError, ValueError, KeyError, RuntimeError) as exc:
                        notes.append(f'無法確認安全狀態或斷開電源：{exc}')
    finally:
        result = summarize(samples, time.monotonic() - started, reason, simulated, notes, fast_samples,
                           [{'pid': pid, 'cmdline': cmd} for pid, cmd in processes.items()])
        write_summary(folder, result)
        print(f"結果：{result['verdict']}；摘要：{folder / 'summary.md'}")
    return {'通過': 0, '不通過': 1, '未判定': 2}[result['verdict']]


if __name__ == '__main__':
    raise SystemExit(main())
