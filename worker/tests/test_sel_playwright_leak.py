"""2026-10-19 23:01 事故回归：批次中途 Supabase RemoteDisconnected → 生成器里的 sync_playwright 没退出 →
之后每次 count 都报 “Playwright Sync API inside the asyncio loop”。这里用真实 Playwright 驱动（不启动浏览器）复现并验证修复。"""
import http.client, io, sys, urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


class QueueEmpty(BaseException): pass


class FakeSelStore:
    """main() 用到的最小 Store；按 runs 队列依次认领。fail_patch_run_heartbeat=N：前 N 次 heartbeat PATCH 抛 RemoteDisconnected。"""
    def __init__(self, runs, batch=None, fail_heartbeat=0):
        self.runs, self.batch, self.fail = list(runs), batch, fail_heartbeat
        self.run_patches, self.batch_patches, self.items = [], [], []
    def purge(self): return {}
    def lookup_purge(self): return 0
    def exclude_words(self): return []
    def claim(self):
        if self.runs: return self.runs.pop(0)
        raise QueueEmpty()   # 常驻循环模式（生产即如此）：队列空 → 结束测试
    def run(self, rid): return {"id": rid, "status": "running", "max_rows": 1000}
    def open_batch(self, rid): return dict(self.batch) if self.batch else None
    def passed_ids(self, rid): return set()
    def patch_run(self, rid, **kw):
        if "heartbeat_at" in kw and self.fail > 0:
            self.fail -= 1; raise http.client.RemoteDisconnected("Remote end closed connection without response")
        self.run_patches.append(kw)
    def patch_run_if(self, rid, expect, **kw): self.patch_run(rid, **kw)   # QA 2026-10-19：查询结果只在仍为 counting 时写回
    def patch_batch(self, bid, **kw):
        self.batch_patches.append(kw)
        if self.batch: self.batch.update(kw)
    def insert_item(self, body): self.items.append(body)
    def new_batch(self, body): pass


RUN_B = {"id": "aae6e428", "keyword": "Resin", "status": "running", "user_id": "u", "params": {}}
RUN_C = {"id": "c78dae12", "keyword": "Resin", "status": "counting", "user_id": "u", "params": {}}
BATCH = {"id": "b1", "batch_no": 1, "offset_start": 1, "offset_end": 40, "scraped_count": 0}


def real_pw_scrape(*a, **kw):
    """与 scrape_ehunt 同构：生成器内 with sync_playwright()，逐行 yield。"""
    with sync_playwright():
        for i in range(1, 41): yield {"listing_id": 1000 + i, "rank": i, "page": (i - 1) // 20 + 1, "title": f"t{i}"}

def real_pw_count(keyword, params):
    with sync_playwright(): return 296


@pytest.fixture
def worker(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "LOOKUPS_ON", False); monkeypatch.setattr(W, "EXCL_DIR", str(tmp_path))
    monkeypatch.setattr(W, "scrape_ehunt", real_pw_scrape); monkeypatch.setattr(W, "count_ehunt", real_pw_count)
    monkeypatch.setattr(W.C, "guard", lambda o: ("排除", "7天销量≤3", "")); monkeypatch.setattr(W.C, "dedupe", lambda rows: rows)
    monkeypatch.setattr(W, "SEEN", {}); monkeypatch.setattr(W, "_sleep", lambda s: None, raising=False)
    def no_exit(code=0): raise SystemExit(code)
    monkeypatch.setattr(W.os, "_exit", no_exit)
    return W


def test_bug_mechanism_unclosed_generator_poisons_sync_playwright():
    """复现根因本身：消费方异常 → 生成器停在 yield → 同线程再 sync_playwright() 必报 asyncio loop。"""
    g = real_pw_scrape(); next(g)
    with pytest.raises(Exception, match="asyncio loop"):
        with sync_playwright(): pass
    assert W.playwright_leaked()
    W.close_scraper(g); assert not W.playwright_leaked()
    with sync_playwright(): pass            # 关闭后恢复


def test_exception_mid_batch_then_count_succeeds(worker, monkeypatch):
    """必须在同一个常驻 main() 循环里（不是 --once）：线上 main 永不返回，失败批次的生成器一直被局部变量引用、不会被 GC 关闭。"""
    st = FakeSelStore([RUN_B, RUN_C], dict(BATCH), fail_heartbeat=1)   # 第 1 次 heartbeat 断连（假 store 不重试）→ 批次失败
    monkeypatch.setattr(W, "Store", lambda: st); monkeypatch.setattr(W, "POLL", 0)
    with pytest.raises(QueueEmpty): W.main([])
    assert not W.playwright_leaked()
    assert st.batch["status"] == "canceled"
    fail = next(p for p in st.run_patches if p.get("status") == "canceled")
    assert "继续本批" in fail["error"] and "RemoteDisconnected" in fail["error"]
    last = [p for p in st.run_patches if "status" in p][-1]   # 忽略进度写入（sel-progress 分支的 progress=）
    assert last["status"] == "counted" and last["total_count"] == 296   # 修复前：asyncio loop 报错 → failed


def test_batch_failure_flushes_and_resumes_from_breakpoint(worker, monkeypatch):
    def flaky(*a, **kw):
        with sync_playwright():
            for i in range(1, 13): yield {"listing_id": 1000 + i, "rank": i, "page": 1, "title": f"t{i}"}
            raise TimeoutError("EHunt page timeout")
    monkeypatch.setattr(W, "scrape_ehunt", flaky)
    st = FakeSelStore([RUN_B], dict(BATCH)); monkeypatch.setattr(W, "Store", lambda: st)
    W.main(["--once"])
    assert st.batch["status"] == "canceled" and st.batch["scraped_count"] == 12   # 已落定的 12 条写回（不是只到 10）
    # 网页「继续本批」(sel_resume) → 批次重新 queued；worker 从 offset_start + scraped_count 接着读
    starts = []
    def resume(kw, params, s0, e0, between=None):
        starts.append(s0); yield from (r for r in real_pw_scrape() if r["rank"] >= s0)
    monkeypatch.setattr(W, "scrape_ehunt", resume)
    st.batch["status"] = "queued"; st.runs = [RUN_B]
    W.main(["--once"])
    assert starts == [13] and st.batch["status"] == "done" and st.batch["scraped_count"] == 40


def test_transient_heartbeat_error_is_retried_not_fatal(worker, monkeypatch):
    """Store._req 自带重试；这里直接测真实 Store：前两次断连、第三次成功。"""
    calls = []
    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    def urlopen(req, timeout=60):
        calls.append(req.get_method())
        if len(calls) <= 2: raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return Resp(b"")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setattr(W.urllib.request, "urlopen", urlopen)
    W.Store().patch_run("r", heartbeat_at="now")
    assert calls == ["PATCH"] * 3


@pytest.mark.parametrize("exc,transient", [
    (http.client.RemoteDisconnected("x"), True), (TimeoutError("t"), True), (ConnectionResetError("r"), True),
    (urllib.error.URLError(OSError("dns")), True),
    (urllib.error.HTTPError("u", 503, "bad", {}, None), True), (urllib.error.HTTPError("u", 429, "rl", {}, None), True),
    (urllib.error.HTTPError("u", 400, "bad", {}, None), False), (urllib.error.HTTPError("u", 404, "nf", {}, None), False),
    (ValueError("v"), False)])
def test_is_transient(exc, transient): assert W.is_transient(exc) is transient


def test_non_transient_and_claim_not_retried(monkeypatch):
    calls = []
    def urlopen(req, timeout=60): calls.append(1); raise http.client.RemoteDisconnected("x")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setattr(W.urllib.request, "urlopen", urlopen); monkeypatch.setattr(W, "_sleep", lambda s: None)
    with pytest.raises(http.client.RemoteDisconnected): W.Store().claim()
    assert len(calls) == 1                                       # 认领非幂等：不重试（超时回收）
    calls.clear()
    def bad(req, timeout=60): calls.append(1); raise urllib.error.HTTPError("u", 400, "bad", {}, None)
    monkeypatch.setattr(W.urllib.request, "urlopen", bad)
    with pytest.raises(urllib.error.HTTPError): W.Store().patch_run("r", x=1)
    assert len(calls) == 1


def test_poisoned_count_requeues_and_exits_for_restart(worker, monkeypatch):
    g = real_pw_scrape(); next(g)            # 人为制造泄漏（模拟未知路径漏关）
    try:
        st = FakeSelStore([RUN_C]); monkeypatch.setattr(W, "Store", lambda: st)
        with pytest.raises(SystemExit) as e: W.main(["--once"])
        assert e.value.code == W.EXIT_RECYCLE
        assert st.run_patches[-1]["status"] == "count_queued"    # 放回队列，新进程立即重新认领，不算失败
    finally: g.close()


def test_leak_detected_after_run_exits_even_without_error(worker, monkeypatch):
    st = FakeSelStore([]); st.claim = lambda: None; monkeypatch.setattr(W, "Store", lambda: st)
    g = real_pw_scrape(); next(g)
    try:
        with pytest.raises(SystemExit) as e: W.main(["--once"])
        assert e.value.code == W.EXIT_RECYCLE
    finally: g.close()
