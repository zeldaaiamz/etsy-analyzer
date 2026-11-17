"""2026-11-17 15:01–15:15 box 断网事故：Page.goto Timeout 60000ms → 多个关键词直接「失败」；doll 批次首页超时后
重开撞上 Chrome「profile is already in use」(ProcessSingleton) → 批次已停止。
保护：① 网络/页面加载超时 = 暂时性，有上限地自动重新排队（2 次）② 连续网络失败熔断：暂停认领 2→5 分钟
③ profile 占用 → 等待重试启动（不杀 chrome、不删锁文件）④ 首页打开失败截图（尽力）。"""
import ssl, sys, urllib.error
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402


class PwTimeout(Exception): pass
PwTimeout.__name__ = "TimeoutError"   # Playwright 的 TimeoutError 类型名


# ---------- 分类：哪些算网络/页面加载暂时性错误 ----------
@pytest.mark.parametrize("e", [
    PwTimeout('Page.goto: Timeout 60000ms exceeded.\nCall log:\n  - navigating to "https://ehunt.ai/etsy-product-research"'),
    RuntimeError("Page.goto: net::ERR_CONNECTION_RESET at https://ehunt.ai/etsy-product-research"),
    RuntimeError("Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://ehunt.ai/"),
    RuntimeError("net::ERR_INTERNET_DISCONNECTED"),
    W.PageTimeout("EHunt 第 1 页读取超时（每次 90 秒，已重试 2 次仍失败）"),
    urllib.error.URLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")),
    ConnectionResetError(104, "Connection reset by peer"),
    RuntimeError("BrowserType.launch_persistent_context: Failed to create a ProcessSingleton for your profile directory."),
])
def test_net_errors_are_temporary(e):
    assert W.is_net_error(e)


@pytest.mark.parametrize("e", [
    RuntimeError("未识别结果数文字：x"),
    RuntimeError("排序不是 7 天销量降序：x"),
    RuntimeError("未识别结果数文字：tassel proxy sslx"),
    W.FilterNotApplied("筛选未生效"),
    W.KeywordNotApplied("关键词未生效"),
    W.RateLimited("EHunt 限流"),
    W.LoginExpired("EHunt 登录已失效") if hasattr(W, "LoginExpired") else RuntimeError("x"),
])
def test_other_errors_not_net(e):
    assert not W.is_net_error(e)


# ---------- ① 查询数量：网络错误有上限地重新排队 ----------
def _count_run(monkeypatch, attempts, exc, breaker=None):
    def boom(kw, params): raise exc
    monkeypatch.setattr(W, "count_ehunt", boom)
    monkeypatch.setattr(W, "NET", breaker or W.NetBreaker(clock=lambda: 0))
    calls = []
    class St:
        def patch_run_if(s, rid, expect, **kw): calls.append(("if", expect, kw))
        def patch_run(s, rid, **kw): calls.append(("run", None, kw))
    class A: fake = None
    W.handle_run(St(), {"id": "r1", "keyword": "table", "status": "counting", "attempts": attempts, "params": {}}, A(), False)
    return calls

GOTO = PwTimeout("Page.goto: Timeout 60000ms exceeded.\nCall log:\n  - navigating to \"https://ehunt.ai/etsy-product-research\"")

@pytest.mark.parametrize("attempts", [1, 2])
def test_count_goto_timeout_requeues(monkeypatch, attempts):
    calls = _count_run(monkeypatch, attempts, GOTO)
    (kind, expect, kw), = [c for c in calls if c[0] == "if"]   # 2026-11-17 ②：重排前先写一次 net_wait 进度（patch_run）
    assert kind == "if" and expect == "counting"
    assert kw["status"] == "count_queued" and kw["locked_by"] is None and kw["error"] is None
    assert "box 网络异常" in kw["progress"]["why"] and f"第 {attempts}/2 次" in kw["progress"]["why"]


def test_count_goto_timeout_fails_after_two_requeues(monkeypatch):
    (kind, expect, kw), = _count_run(monkeypatch, 3, GOTO)
    assert kw["status"] == "failed" and "box 网络异常" in kw["error"] and "已自动重试 2 次" in kw["error"]
    assert "网页卡住或改版" not in kw["error"]


def test_count_net_failure_feeds_breaker_and_success_resets(monkeypatch):
    br = W.NetBreaker(clock=lambda: 0)
    _count_run(monkeypatch, 1, GOTO, br); assert br.streak == 1
    monkeypatch.setattr(W, "count_ehunt", lambda kw, p: 5)
    class St:
        def patch_run_if(s, rid, expect, **kw): pass
    class A: fake = None
    W.handle_run(St(), {"id": "r2", "keyword": "x", "status": "counting", "attempts": 1, "params": {}}, A(), False)
    assert br.streak == 0


def test_count_filter_not_applied_unchanged(monkeypatch):
    (kind, expect, kw), = _count_run(monkeypatch, 1, W.FilterNotApplied("筛选未生效，Selected: 缺少 ['Price: 8~40']"))
    assert kw["status"] == "count_queued" and "筛选未生效" in kw["progress"]["why"]


# ---------- ① 抓取批次：网络错误 → 批次放回队列（不标已停止），第 3 次才停 ----------
def _batch_run(monkeypatch, tmp_path, exc, progress=None, status="running"):
    monkeypatch.setattr(W, "EXCL_DIR", str(tmp_path)); monkeypatch.setattr(W, "SEEN", {})
    monkeypatch.setattr(W, "NET", W.NetBreaker(clock=lambda: 0))
    def scrape(*a, **k):
        raise exc
        yield  # noqa
    monkeypatch.setattr(W, "scrape_ehunt", scrape)
    rp, rpi, bp = [], [], []
    class St:
        def open_batch(s, rid): return {"id": "b1", "batch_no": 1, "offset_start": 1, "offset_end": 188, "scraped_count": 0}
        def run(s, rid): return {"id": rid, "status": status, "max_rows": 188}
        def passed_ids(s, rid): return set()
        def patch_run(s, rid, **kw): rp.append(kw)
        def patch_run_if(s, rid, expect, **kw): rpi.append((expect, kw))
        def patch_batch(s, bid, **kw): bp.append(kw)
        def insert_item(s, b): pass
    class A: fake = None
    W.handle_run(St(), {"id": "r1", "keyword": "doll", "status": "running", "user_id": "u", "params": {}, "progress": progress}, A(), False)
    return rp, rpi, bp


def test_batch_goto_timeout_requeues_batch(monkeypatch, tmp_path):
    rp, rpi, bp = _batch_run(monkeypatch, tmp_path, GOTO)
    (expect, kw), = rpi
    assert expect == "running" and kw["status"] == "queued" and kw["locked_by"] is None
    assert kw["progress"]["net_requeues"] == 1 and kw["progress"]["batch_id"] == "b1"
    assert bp[-1]["status"] == "queued" and not any(k.get("status") == "canceled" for k in rp + bp)


def test_batch_second_requeue_counts_from_progress(monkeypatch, tmp_path):
    rp, rpi, bp = _batch_run(monkeypatch, tmp_path, GOTO, progress={"stage": "requeued", "net_requeues": 1, "batch_id": "b1"})
    (expect, kw), = rpi
    assert kw["progress"]["net_requeues"] == 2 and "第 2/2 次" in kw["progress"]["why"]


def test_batch_counter_ignores_other_batch(monkeypatch, tmp_path):
    rp, rpi, bp = _batch_run(monkeypatch, tmp_path, GOTO, progress={"stage": "requeued", "net_requeues": 2, "batch_id": "old"})
    (expect, kw), = rpi
    assert kw["progress"]["net_requeues"] == 1


def test_batch_third_net_failure_stops_with_note(monkeypatch, tmp_path):
    rp, rpi, bp = _batch_run(monkeypatch, tmp_path, GOTO, progress={"stage": "requeued", "net_requeues": 2, "batch_id": "b1"})
    assert not rpi
    fin = [k for k in rp if k.get("status") == "canceled"][-1]
    assert "box 网络异常" in fin["error"] and "已自动重试 2 次" in fin["error"] and "继续本批" in fin["error"]
    assert bp[-1]["status"] == "canceled"


def test_batch_net_failure_when_user_paused_does_not_requeue(monkeypatch, tmp_path):
    rp, rpi, bp = _batch_run(monkeypatch, tmp_path, GOTO, status="paused")
    assert not rpi and bp[-1]["status"] == "canceled"


def test_batch_profile_in_use_requeues(monkeypatch, tmp_path):
    rp, rpi, bp = _batch_run(monkeypatch, tmp_path, RuntimeError(
        "BrowserType.launch_persistent_context: Failed to create a ProcessSingleton for your profile directory. profile is already in use"))
    assert rpi and rpi[0][1]["status"] == "queued"


# ---------- ② 熔断 ----------
def test_breaker_trips_after_three_and_escalates():
    t = [0.0]; br = W.NetBreaker(clock=lambda: t[0])
    assert (W.NET_BREAKER_THRESHOLD, W.NET_BREAKER_COOLDOWN_S, W.NET_BREAKER_MAX_S) == (3, 120, 300)
    br.fail("x"); br.fail("x"); assert not br.is_open()
    br.fail("x"); assert br.is_open() and br.remaining() == pytest.approx(120)
    t[0] = 119; assert br.is_open()
    t[0] = 121; assert not br.is_open()
    br.fail("x"); assert br.is_open() and br.remaining() == pytest.approx(240)   # 恢复后又失败：立即再熔断，冷却翻倍
    t[0] += 241; br.fail("x"); assert br.remaining() == pytest.approx(300)       # 上限 5 分钟
    t[0] += 301; br.ok(); assert br.streak == 0 and br.cooldown == 120 and not br.is_open()


def test_main_loop_skips_claim_while_breaker_open(monkeypatch):
    t = [0.0]; br = W.NetBreaker(clock=lambda: t[0])
    for _ in range(3): br.fail("x")
    monkeypatch.setattr(W, "NET", br); monkeypatch.setattr(W, "LOOKUPS_ON", False); monkeypatch.setattr(W, "KEEPALIVE_ON", False)
    claims = []
    class Stop(BaseException): pass
    class St:
        def purge(s): return {}
        def lookup_purge(s): return 0
        def exclude_words(s): return []
        def claim(s): claims.append(t[0]); raise Stop()
    monkeypatch.setattr(W, "Store", St); monkeypatch.setattr(W, "purge_local", lambda: 0)
    sleeps = []
    def fake_sleep(s):
        sleeps.append(s); t[0] += s
    monkeypatch.setattr(W.time, "sleep", fake_sleep)
    with pytest.raises(Stop): W.main(["--fake", "x.json"])
    assert claims and claims[0] >= 120 and sleeps   # 冷却结束前没有认领


def test_main_loop_db_ssl_error_on_claim_feeds_breaker_not_crash(monkeypatch):
    br = W.NetBreaker(clock=lambda: 0)
    monkeypatch.setattr(W, "NET", br); monkeypatch.setattr(W, "LOOKUPS_ON", False); monkeypatch.setattr(W, "KEEPALIVE_ON", False)
    n = [0]
    class Stop(BaseException): pass
    class St:
        def purge(s): return {}
        def lookup_purge(s): return 0
        def exclude_words(s): return []
        def claim(s):
            n[0] += 1
            if n[0] <= 2: raise urllib.error.URLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"))
            raise Stop()
    monkeypatch.setattr(W, "Store", St); monkeypatch.setattr(W, "purge_local", lambda: 0)
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    with pytest.raises(Stop): W.main(["--fake", "x.json"])
    assert n[0] == 3 and br.streak == 2


# ---------- ③ Chrome profile 占用：等待重试，不杀进程 ----------
BUSY = "BrowserType.launch_persistent_context: Failed to create a ProcessSingleton for your profile directory. This usually means that the profile is already in use by another instance of Chromium."

def _launcher(fail_n, msg=BUSY):
    seen = []
    class Chromium:
        def launch_persistent_context(self, profile, **kw):
            seen.append(1)
            if len(seen) <= fail_n: raise RuntimeError(msg)
            return "ctx"
    class P: chromium = Chromium()
    return P(), seen


def test_launch_waits_when_profile_in_use(monkeypatch):
    t = [0.0]; sl = []
    monkeypatch.setattr(W, "_launch_sleep", lambda s: (sl.append(s), t.__setitem__(0, t[0] + s)))
    monkeypatch.setattr(W, "_launch_clock", lambda: t[0])
    monkeypatch.setattr(W, "_kill_profile_browser", lambda *a, **k: pytest.fail("must not kill chrome"))
    p, seen = _launcher(2)
    assert W.launch_ctx(p, apply_state=False) == "ctx" and len(seen) == 3 and sl == [W.LAUNCH_BUSY_WAIT_S] * 2


def test_launch_gives_up_after_budget(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(W, "_launch_sleep", lambda s: t.__setitem__(0, t[0] + s))
    monkeypatch.setattr(W, "_launch_clock", lambda: t[0])
    p, seen = _launcher(999)
    with pytest.raises(RuntimeError, match="ProcessSingleton"): W.launch_ctx(p, apply_state=False)
    assert t[0] <= W.LAUNCH_BUSY_MAX_S and len(seen) >= 5


def test_launch_other_error_not_retried(monkeypatch):
    monkeypatch.setattr(W, "_launch_sleep", lambda s: pytest.fail("no wait"))
    p, seen = _launcher(999, "other failure")
    with pytest.raises(RuntimeError, match="other"): W.launch_ctx(p, apply_state=False)
    assert len(seen) == 1


def test_launch_busy_constants():
    assert (W.LAUNCH_BUSY_WAIT_S, W.LAUNCH_BUSY_MAX_S) == (8, 60)


# ---------- ④ 首页打开失败：截图（尽力）+ 关上下文 ----------
class GotoFailPage:
    def __init__(self, shot_ok=True): self.shots, self.shot_ok, self.url = [], shot_ok, "about:blank"
    def goto(self, *a, **k): raise GOTO
    def screenshot(self, path=None, **k):
        if not self.shot_ok: raise RuntimeError("screenshot failed")
        self.shots.append(path)


class Ctx:
    def __init__(self, pg): self.pages, self.closed = [pg], False
    def close(self): self.closed = True


@pytest.mark.parametrize("shot_ok", [True, False])
def test_open_list_goto_failure_screenshots_and_closes(monkeypatch, tmp_path, shot_ok):
    pg = GotoFailPage(shot_ok); ctx = Ctx(pg)
    monkeypatch.setattr(W, "launch_ctx", lambda p, **k: ctx); monkeypatch.setattr(W, "LOGDIR", str(tmp_path))
    with pytest.raises(PwTimeout): W._open_list(object(), "table", {})
    assert ctx.closed
    if shot_ok: assert len(pg.shots) == 1 and "ehunt-goto-fail-" in pg.shots[0] and pg.shots[0].startswith(str(tmp_path))


def test_main_loop_survives_db_error_after_claim(monkeypatch):
    br = W.NetBreaker(clock=lambda: 0)
    monkeypatch.setattr(W, "NET", br); monkeypatch.setattr(W, "LOOKUPS_ON", False); monkeypatch.setattr(W, "KEEPALIVE_ON", False)
    n = [0]
    class Stop(BaseException): pass
    class St:
        def purge(s): return {}
        def lookup_purge(s): return 0
        def exclude_words(s): return []
        def claim(s):
            n[0] += 1
            if n[0] == 1: return {"id": "r1", "keyword": "doll", "status": "running", "params": {}}
            raise Stop()
        def open_batch(s, rid): raise urllib.error.URLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"))
    monkeypatch.setattr(W, "Store", St); monkeypatch.setattr(W, "purge_local", lambda: 0)
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    with pytest.raises(Stop): W.main(["--fake", "x.json"])
    assert br.streak == 1 and n[0] == 2
