import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.soak_test import parse_duration, summarize, output_active


def sample(t, rss, connected=True, age=1):
    return dict(elapsed=t, rss_mb=rss, threads=5, handles=10, psu_connected=connected,
                psu_phase='idle' if connected else 'disconnected', psu_fail_count=0,
                psu_error='', last_ok_age=age, sensors={}, http_latency_ms=10, http_error='')


def test_duration():
    assert parse_duration('3h') == 10800
    assert parse_duration('90m') == 5400
    assert parse_duration('600s') == 600
    with pytest.raises(ValueError):
        parse_duration('0m')


def test_rss_criteria():
    assert summarize([sample(0, 100), sample(3600, 106)], 3600)['criteria']['rss']['status'] == '不通過'
    assert summarize([sample(0, 100), sample(3600, 101)], 3600)['criteria']['rss']['status'] == '通過'
    assert summarize([sample(0, 100), sample(100, 101)], 100)['criteria']['rss']['status'] == '時間不足'


def test_disconnect_and_age():
    result = summarize([sample(0, 100), sample(5, 100, False, 3), sample(10, 100)], 10)
    assert result['psu']['disconnects'] == 1
    assert result['psu']['max_last_ok_age'] == 3
    assert result['criteria']['communication']['status'] == '不通過'


@pytest.mark.parametrize('phase', ['relays', 'powering', 'on', 'stopping'])
def test_output_abort(phase):
    assert output_active(phase)
    assert not output_active('idle')



def test_verdict_three_states():
    good = [sample(0, 100), sample(10, 100), sample(20, 100)]
    assert summarize(good, 20)['verdict'] == '未判定'
    assert summarize(good, 1800)['verdict'] == '通過'
    assert summarize(good, 20, aborted='Ctrl+C')['verdict'] == '不通過'
    good[1]['http_error'] = 'error'
    assert summarize(good, 20)['verdict'] == '不通過'


def test_process_tree_sum():
    from scripts.soak_test import process_metrics
    class Proc:
        def __init__(self, pid, children=()): self.pid, self._children = pid, children
        def children(self, recursive=False): return self._children
        def memory_info(self): return type('Memory', (), {'rss': self.pid * 1024 ** 2})()
        def cpu_percent(self): return self.pid * 2
        def num_threads(self): return self.pid + 1
        def num_fds(self): return self.pid + 2
        def cmdline(self): return ['python', f'{self.pid}.py']
    result = process_metrics(Proc(1, [Proc(2), Proc(3)]), [])
    assert (result['rss_mb'], result['cpu_percent'], result['threads'], result['handles']) == (6, 12, 9, 12)


def test_fast_psu_statistics_and_events():
    from scripts.soak_test import psu_observation
    previous = None
    observations = []
    events = []
    for now, fail, phase, connected, last_ok in [(10, 0, 'idle', True, 9), (11, 1, 'idle', True, 9), (12, 2, 'disconnected', False, 9), (13, 0, 'idle', True, 13)]:
        obs, changes = psu_observation({'connected': connected, 'phase': phase, 'fail_count': fail, 'last_ok': last_ok}, now, previous)
        observations.append(obs)
        events.extend(changes)
        previous = obs
    result = summarize([sample(0, 100)], 10, psu_samples=observations)
    assert result['psu']['max_last_ok_age'] == 3
    assert result['psu']['fail_count_positive_samples'] == 2
    assert sum(name == '電源通訊失敗' for name, _ in events) == 2
    assert result['psu']['disconnects'] == 1


def test_find_process_fallback_uses_port_and_topmost(monkeypatch):
    from scripts import soak_test
    class Proc:
        def __init__(self, pid, ppid, cmd):
            self.pid = pid
            self.info = {'pid': pid, 'ppid': ppid, 'cmdline': cmd}
    processes = [Proc(1, 0, ['uv', 'run', 'python', 'main.py', '--port', '5099']),
                 Proc(2, 1, ['python', 'main.py', '--port', '5099']),
                 Proc(3, 0, ['python', 'main.py', '--port', '5002'])]
    def denied(**kwargs):
        raise PermissionError('denied')
    monkeypatch.setattr(soak_test.psutil, 'net_connections', denied)
    monkeypatch.setattr(soak_test.psutil, 'process_iter', lambda attrs: processes)
    assert soak_test.find_process('http://localhost:5099', None, []).pid == 1


def test_find_process_handles_denied_process_scan(monkeypatch):
    from scripts import soak_test
    def denied(*args, **kwargs):
        raise PermissionError('denied')
    monkeypatch.setattr(soak_test.psutil, 'net_connections', denied)
    monkeypatch.setattr(soak_test.psutil, 'process_iter', denied)
    notes = []
    assert soak_test.find_process('http://localhost:5099', None, notes) is None
    assert any('無法掃描程序' in note for note in notes)
