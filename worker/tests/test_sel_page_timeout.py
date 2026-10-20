"""2026-10-20 22:59 事故：第 14 页 reading rows 卡 637s 堵住队列 → 单页硬超时 + 重开同一页重试。用假 session/假时钟，不开浏览器。"""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402

PS = 20

class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t

class NoDog:
    fired = False
    def disarm(self): pass

class FakeSite:
    """hang_plan: {page: 次数} —— 该页前 N 次读取“卡住”（时钟越过 deadline 后抛 PageTimeout，模拟 read 内的超时检查）。"""
    def __init__(self, clock, total=60, hang_plan=None, hang_mid=False, exc=None):
        self.clock, self.total, self.hang = clock, total, dict(hang_plan or {})
        self.hang_mid, self.exc = hang_mid, exc
        self.opens, self.closes, self.reads = [], 0, []
    def open(self, page):
        self.opens.append(page); site = self
        class S:
            ctx = object(); cur = page
            def next(s, n, deadline): s.cur = n; site.clock.t += 2
            def read(s, n, deadline):
                assert s.cur == n; site.reads.append(n)
                if site.hang.get(n, 0) > 0:
                    site.hang[n] -= 1; site.clock.t = deadline + 1
                    if site.exc: raise site.exc
                    raise W.PageTimeout("本页读取超时")
                site.clock.t += 10
                ranks = range((n - 1) * PS + 1, min(n * PS, site.total) + 1)
                return [(r, {"rank": r, "page": n, "listing_id": str(1000 + r)}) for r in ranks], n * PS < site.total
            def close(s): site.closes += 1
        return S()

def run(site, clock, start=1, end=60, retries=2, between=None):
    return list(W.page_loop(site.open, start, end, PS, between=between, timeout=90, retries=retries, clock=clock, watchdog=lambda s: NoDog()))

def test_normal_pages_no_retry():
    c = Clock(); s = FakeSite(c); rows = run(s, c)
    assert [r["rank"] for r in rows] == list(range(1, 61)); assert s.opens == [1] and s.reads == [1, 2, 3] and s.closes == 1

def test_hang_then_retry_succeeds_same_page_no_dups(caplog):
    c = Clock(); s = FakeSite(c, hang_plan={2: 1}); seen = []
    rows = run(s, c, between=lambda ctx: seen.append(ctx))
    ranks = [r["rank"] for r in rows]
    assert ranks == list(range(1, 61)) and len(set(ranks)) == 60          # 无重复、无缺失
    assert s.opens == [1, 2]                                              # 重开直接回到第 2 页（同一 rank 偏移）
    assert s.reads == [1, 2, 2, 3] and s.closes == 2
    assert "timed out" in caplog.text and len(seen) == 3                  # 每页读完仍穿插主图查询

def test_resume_midpage_start_rank_respected():
    c = Clock(); s = FakeSite(c, hang_plan={14: 2}, total=300)
    rows = run(s, c, start=261, end=300)
    assert [r["rank"] for r in rows] == list(range(261, 301)) and s.opens == [14, 14, 14]

def test_hang_all_retries_clean_failure():
    c = Clock(); s = FakeSite(c, hang_plan={2: 99}); got = []
    with pytest.raises(W.PageTimeout, match="第 2 页读取超时.*已重试 2 次"):
        for r in W.page_loop(s.open, 1, 60, PS, timeout=90, retries=2, clock=c, watchdog=lambda x: NoDog()): got.append(r["rank"])
    assert got == list(range(1, 21)) and s.reads == [1, 2, 2, 2] and s.closes == len(s.opens) == 3   # 浏览器全部关闭

def test_playwright_timeout_error_also_retried():
    class TimeoutError_(Exception): pass
    TimeoutError_.__name__ = "TimeoutError"
    c = Clock(); s = FakeSite(c, hang_plan={1: 1}, exc=TimeoutError_("Timeout 30000ms exceeded"))
    assert len(run(s, c)) == 60

def test_login_expired_not_retried():
    c = Clock(); s = FakeSite(c, hang_plan={1: 1}, exc=W.LoginExpired("x"))
    with pytest.raises(W.LoginExpired): run(s, c)
    assert s.opens == [1]

def test_other_errors_not_retried():
    c = Clock(); s = FakeSite(c)
    def boom(page): raise ValueError("selector broke")
    with pytest.raises(ValueError): list(W.page_loop(boom, 1, 20, PS, timeout=90, retries=2, clock=c, watchdog=lambda x: NoDog()))

def test_watchdog_fires_and_disarms():
    import threading, time
    ev = threading.Event(); d = W._Watchdog(0.05, ev.set); assert ev.wait(2) and d.fired
    ev2 = threading.Event(); d2 = W._Watchdog(0.2, ev2.set); d2.disarm(); time.sleep(0.3); assert not ev2.is_set()

def test_real_slow_page_with_real_clock():
    """真实时钟：一次真的“卡住”（sleep 超过 deadline）→ 超时 → 重试成功。"""
    import time
    calls = []
    def open_(page):
        class S:
            ctx = None
            def next(s, n, d): pass
            def read(s, n, deadline):
                calls.append(n)
                if len(calls) == 1:
                    while time.monotonic() < deadline + 0.05: time.sleep(0.01)
                    raise W.PageTimeout("hang")
                return [(1, {"rank": 1})], False
            def close(s): pass
        return S()
    assert [r["rank"] for r in W.page_loop(open_, 1, 1, PS, timeout=0.1, retries=2, watchdog=lambda x: NoDog())] == [1] and calls == [1, 1]

def test_page_timeout_fails_batch_cleanly_with_chinese_reason(monkeypatch, tmp_path):
    """全部重试失败 → handle_run 现有路径：批次“已停止”、run canceled + 中文原因（可「继续本批」），不卡、不 recycle。"""
    monkeypatch.setattr(W, "EXCL_DIR", str(tmp_path)); monkeypatch.setattr(W, "SEEN", {})
    monkeypatch.setattr(W.C, "guard", lambda o: ("排除", "7天销量≤3", "")); monkeypatch.setattr(W.C, "dedupe", lambda rows: rows)
    def scrape(kw, params, s0, e0, between=None):
        c = Clock(); site = FakeSite(c, hang_plan={2: 99})
        yield from W.page_loop(site.open, s0, e0, PS, timeout=90, retries=2, clock=c, watchdog=lambda x: NoDog())
    monkeypatch.setattr(W, "scrape_ehunt", scrape)
    rp, bp = [], []
    class St:
        def open_batch(s, rid): return {"id": "b1", "batch_no": 1, "offset_start": 1, "offset_end": 60, "scraped_count": 0}
        def run(s, rid): return {"id": rid, "status": "running", "max_rows": 60}
        def passed_ids(s, rid): return set()
        def patch_run(s, rid, **kw): rp.append(kw)
        def patch_batch(s, bid, **kw): bp.append(kw)
        def insert_item(s, b): pass
    class A: fake = None
    W.handle_run(St(), {"id": "r1", "keyword": "tag", "status": "running", "user_id": "u", "params": {}}, A(), False)
    fin = [k for k in rp if k.get("status") == "canceled"][-1]
    assert "第 2 页读取超时" in fin["error"] and "继续本批" in fin["error"]
    assert any(k.get("status") == "canceled" for k in bp) and any(k.get("scraped_count") == 20 for k in bp)   # 第 1 页已落定的 20 条保留

def test_kill_only_own_descendant_with_profile(tmp_path):
    import subprocess, time
    prof = tmp_path / "prof"; other = tmp_path / "other"
    a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", f"--user-data-dir={prof}"])
    b = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", f"--user-data-dir={other}"])
    try:
        time.sleep(0.2); killed = W._kill_profile_browser(str(prof))
        assert killed == [a.pid] and a.wait(5) == -9 and b.poll() is None
    finally: b.kill(); a.kill()
