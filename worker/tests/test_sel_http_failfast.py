"""2026-10-23：Supabase urlopen timeout=60 + SSL/TimeoutError 堵主循环/心跳 → UI 显示 offline。"""
import io, http.client, sys, time, urllib.error
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setattr(W, "_sleep", lambda s: None)
    return W.Store()


def test_http_timeout_default_is_failfast_not_60():
    assert W.HTTP_TIMEOUT <= 15 and W.HB_TIMEOUT <= 8 and W.HTTP_TIMEOUT < 60


def test_heartbeat_uses_short_timeout(store, monkeypatch):
    seen = []
    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    def urlopen(req, timeout=60):
        seen.append(timeout); return Resp(b"")
    monkeypatch.setattr(W.urllib.request, "urlopen", urlopen)
    store.heartbeat({"pid": 1, "phase": "idle"})
    assert seen == [W.HB_TIMEOUT]


def test_patch_uses_http_timeout_not_60(store, monkeypatch):
    seen = []
    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    def urlopen(req, timeout=60):
        seen.append(timeout); return Resp(b"")
    monkeypatch.setattr(W.urllib.request, "urlopen", urlopen)
    store.patch_run("r", heartbeat_at="now")
    assert seen == [W.HTTP_TIMEOUT]


def test_timeout_error_retries_briefly_then_raises(store, monkeypatch):
    """每次短超时；退避上限 BACKOFF_CAP，总阻塞远小于旧的 5×60s。"""
    calls, sleeps = [], []
    def urlopen(req, timeout=60):
        calls.append(timeout); raise TimeoutError("The read operation timed out")
    monkeypatch.setattr(W.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(W, "_sleep", lambda s: sleeps.append(s))
    with pytest.raises(TimeoutError):
        store.patch_run("r", x=1)
    assert len(calls) == W.RETRIES and all(t == W.HTTP_TIMEOUT for t in calls)
    assert sleeps and max(sleeps) <= W.BACKOFF_CAP + W.BACKOFF_BASE + 0.01


def test_heartbeat_timeout_not_retried(store, monkeypatch):
    calls = []
    def urlopen(req, timeout=60):
        calls.append(timeout); raise TimeoutError("The read operation timed out")
    monkeypatch.setattr(W.urllib.request, "urlopen", urlopen)
    with pytest.raises(TimeoutError):
        store.heartbeat({"pid": 1})
    assert calls == [W.HB_TIMEOUT]   # retries=1：失败即跳过，心跳线程继续 sleep
