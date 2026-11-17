"""2026-11-17 Hongyan 批准（预览）：EHunt 选品 worker 三项网络修复。
① 就绪判定：不再等 networkidle（网络稍卡就 60 秒超时），页面主体 + 搜索框 + 结果列表出现就开始读；登录页/登录弹窗单独识别（不当网络问题）
② 网络类失败：等 1→2 分钟后自动重试（最多 2 次），仍失败 → 「box 网络异常」；登录失效文案保持独立
③ 网络自检：心跳连续失败（SSL / 断网）→ 暂停认领新任务，心跳恢复后自动继续，并写日志。
全部用模拟页面 / 模拟网络失败，不开真实 EHunt。"""
import re, ssl, sys, urllib.error, logging
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402

SRC = (Path(__file__).resolve().parents[1] / "selection" / "ehunt_worker.py").read_text()


class PwTimeout(Exception): pass
PwTimeout.__name__ = "TimeoutError"
GOTO = PwTimeout('Page.goto: Timeout 45000ms exceeded.\nCall log:\n  - navigating to "https://ehunt.ai/etsy-product-research"')


# ======================= ① 就绪判定 =======================
class Loc:
    def __init__(self, n): self.n = n
    def count(self): return self.n
    @property
    def first(self): return self
    def is_visible(self): return self.n > 0
    def all(self): return [self] * self.n
    def filter(self, **k): return self
    def nth(self, i): return self


class FakePage:
    """按时间线出现元素：timeline = {selector_key: 出现时刻（秒）}；url_at = [(t, url)]。"""
    def __init__(self, U, clock, timeline, url="https://ehunt.ai/etsy-product-research", url_at=()):
        self.U, self.clock, self.tl, self._url, self.url_at, self.gotos = U, clock, timeline, url, list(url_at), []
    @property
    def url(self):
        u = self._url
        for t, x in self.url_at:
            if self.clock() >= t: u = x
        return u
    def goto(self, url, **kw): self.gotos.append((url, kw))
    def locator(self, sel):
        for k, t in self.tl.items():
            if self.U[k] == sel: return Loc(1 if self.clock() >= t else 0)
        return Loc(0)
    def query_selector(self, sel): return None
    def wait_for_load_state(self, state, timeout=None):
        if state == "networkidle": raise PwTimeout(f"Timeout {timeout}ms exceeded.")


@pytest.fixture
def clk(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(W, "_ready_clock", lambda: t[0])
    monkeypatch.setattr(W, "_ready_sleep", lambda s: t.__setitem__(0, t[0] + s))
    return t


def test_goto_ready_uses_domcontentloaded_not_networkidle(clk):
    U = W.ui(); pg = FakePage(U, lambda: clk[0], {"search_input": 1.0, "row": 2.0})
    assert W.goto_ready(pg, U) == "ready"
    (url, kw), = pg.gotos
    assert url == U["url"] and kw["wait_until"] == "domcontentloaded"
    assert clk[0] == pytest.approx(2.0, abs=0.6)   # 列表一出现就开始，不等网络安静


def test_ready_without_rows_after_grace(clk):
    U = W.ui(); pg = FakePage(U, lambda: clk[0], {"search_input": 1.0})
    assert W.goto_ready(pg, U) == "ready"
    assert clk[0] == pytest.approx(1.0 + W.ROWS_GRACE_S, abs=0.6) and clk[0] < W.READY_TIMEOUT_S


def test_login_redirect_is_login_not_network(clk):
    U = W.ui(); pg = FakePage(U, lambda: clk[0], {"search_input": 1.0}, url_at=[(2.0, "https://ehunt.ai/login?redirect=x")])
    assert W.goto_ready(pg, U) == "login" and clk[0] < 3


def test_login_dialog_is_login(clk):
    U = W.ui(); pg = FakePage(U, lambda: clk[0], {"search_input": 1.0, "login_dialog": 1.5})
    assert W.goto_ready(pg, U) == "login"


def test_nothing_appears_raises_net_type_error(clk):
    U = W.ui(); pg = FakePage(U, lambda: clk[0], {})
    with pytest.raises(W.PageNotReady) as ei: W.goto_ready(pg, U)
    assert W.is_net_error(ei.value) and not isinstance(ei.value, W.LoginExpired)
    assert clk[0] == pytest.approx(W.READY_TIMEOUT_S, abs=0.6)


def test_header_login_only_at_deadline_counts_as_login(clk):
    """顶栏 Login 可能在水合前闪一下：只在超时仍没有就绪时才算登录问题（交给 _check_login 严格判定），不提前判掉线。"""
    U = W.ui(); pg = FakePage(U, lambda: clk[0], {"header_login": 0.5, "search_input": 3.0, "row": 4.0})
    assert W.goto_ready(pg, U) == "ready"
    pg2 = FakePage(U, lambda: clk[0] - clk[0], {"header_login": 0.0})   # 一直只有顶栏 Login，没有搜索框
    clk[0] = 0.0
    assert W.goto_ready(pg2, U) == "login"


def test_ready_timeout_is_sane():
    assert 20 <= W.READY_TIMEOUT_S <= 60 and 3 <= W.ROWS_GRACE_S <= 15


def test_settle_swallows_networkidle_timeout(clk):
    U = W.ui(); pg = FakePage(U, lambda: clk[0], {})
    W._settle(pg)   # 不抛


def test_no_hard_networkidle_waits_left_in_worker():
    assert 'wait_until="networkidle"' not in SRC
    idle = [l for l in SRC.splitlines() if 'wait_for_load_state("networkidle"' in l]
    # 唯一允许的 networkidle：_settle 内部（尽力等待、吞掉超时）
    assert len(idle) == 1 and idle[0].strip().startswith("try:"), idle


# ======================= ② 延迟重试 + 文案 =======================
@pytest.fixture
def nsleep(monkeypatch):
    s = []; monkeypatch.setattr(W, "_net_sleep", lambda x: s.append(x)); return s


def _count(monkeypatch, attempts, exc, status_seq=None):
    monkeypatch.setattr(W, "count_ehunt", lambda kw, p: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(W, "NET", W.NetBreaker(clock=lambda: 0))
    calls, seq = [], list(status_seq or [])
    class St:
        def patch_run_if(s, rid, expect, **kw): calls.append(("if", expect, kw))
        def patch_run(s, rid, **kw): calls.append(("run", None, kw))
        def run(s, rid): return {"id": rid, "status": seq.pop(0) if seq else "counting"}
    class A: fake = None
    W.handle_run(St(), {"id": "r1", "keyword": "table", "status": "counting", "attempts": attempts, "params": {}}, A(), False)
    return calls


def test_delays_are_one_then_two_minutes():
    assert tuple(W.NET_RETRY_DELAYS_S) == (60, 120) and W.NET_REQUEUES == 2 == W.COUNT_FILTER_REQUEUES


@pytest.mark.parametrize("attempts,delay", [(1, 60), (2, 120)])
def test_count_net_error_waits_then_requeues(monkeypatch, nsleep, attempts, delay):
    calls = _count(monkeypatch, attempts, GOTO)
    assert sum(nsleep) == pytest.approx(delay)
    waits = [kw for k, _, kw in calls if k == "run" and (kw.get("progress") or {}).get("stage") == "net_wait"]
    assert waits and "box 网络异常" in waits[0]["progress"]["why"] and f"第 {attempts}/2 次" in waits[0]["progress"]["why"]
    (_, expect, kw), = [c for c in calls if c[0] == "if"]
    assert expect == "counting" and kw["status"] == "count_queued" and kw["error"] is None
    assert "box 网络异常" in kw["progress"]["why"]


def test_count_final_failure_says_box_network(monkeypatch, nsleep):
    calls = _count(monkeypatch, 3, GOTO)
    (_, _, kw), = [c for c in calls if c[0] == "if"]
    assert kw["status"] == "failed" and kw["error"].startswith("box 网络异常") and "已自动重试 2 次" in kw["error"]
    assert "EHunt 连接超时" not in kw["error"] and not nsleep


def test_count_user_cancel_during_wait_stops_without_requeue(monkeypatch, nsleep):
    calls = _count(monkeypatch, 1, GOTO, status_seq=["canceled"])
    assert not [c for c in calls if c[0] == "if"] and sum(nsleep) < 60


def test_count_login_loss_message_stays_distinct(monkeypatch, nsleep):
    seen = []
    monkeypatch.setattr(W, "pause_for_login", lambda store, run, e, batch=None: seen.append(str(e)))
    _count(monkeypatch, 1, W.LoginExpired(W.LOGIN_MSG))
    assert seen and "登录已失效" in seen[0] and "box 网络异常" not in seen[0] and not nsleep


def test_count_filter_not_applied_no_delay(monkeypatch, nsleep):
    calls = _count(monkeypatch, 1, W.FilterNotApplied("筛选未生效"))
    (_, _, kw), = [c for c in calls if c[0] == "if"]
    assert kw["status"] == "count_queued" and "筛选未生效" in kw["progress"]["why"] and not nsleep


def _batch(monkeypatch, tmp_path, exc, progress=None):
    monkeypatch.setattr(W, "EXCL_DIR", str(tmp_path)); monkeypatch.setattr(W, "SEEN", {})
    monkeypatch.setattr(W, "NET", W.NetBreaker(clock=lambda: 0))
    def scrape(*a, **k):
        raise exc
        yield  # noqa
    monkeypatch.setattr(W, "scrape_ehunt", scrape)
    rp, rpi, bp = [], [], []
    class St:
        def open_batch(s, rid): return {"id": "b1", "batch_no": 1, "offset_start": 1, "offset_end": 188, "scraped_count": 0}
        def run(s, rid): return {"id": rid, "status": "running", "max_rows": 188}
        def passed_ids(s, rid): return set()
        def patch_run(s, rid, **kw): rp.append(kw)
        def patch_run_if(s, rid, expect, **kw): rpi.append((expect, kw))
        def patch_batch(s, bid, **kw): bp.append(kw)
        def insert_item(s, b): pass
    class A: fake = None
    W.handle_run(St(), {"id": "r1", "keyword": "doll", "status": "running", "user_id": "u", "params": {}, "progress": progress}, A(), False)
    return rp, rpi, bp


def test_batch_net_error_waits_then_requeues(monkeypatch, tmp_path, nsleep):
    rp, rpi, bp = _batch(monkeypatch, tmp_path, GOTO)
    assert sum(nsleep) == pytest.approx(60)
    (expect, kw), = rpi
    assert kw["status"] == "queued" and "box 网络异常" in kw["progress"]["why"]


def test_batch_final_failure_says_box_network(monkeypatch, tmp_path, nsleep):
    rp, rpi, bp = _batch(monkeypatch, tmp_path, GOTO, progress={"net_requeues": 2, "batch_id": "b1"})
    fin = [k for k in rp if k.get("status") == "canceled"][-1]
    assert "box 网络异常" in fin["error"] and "已自动重试 2 次" in fin["error"] and "继续本批" in fin["error"]
    assert "EHunt 连接超时" not in fin["error"]


# ======================= ③ 网络自检（心跳） =======================
SSL_ERR = urllib.error.URLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"))


def test_heartbeat_failures_hold_claims_until_recovery(caplog):
    t = [0.0]; br = W.NetBreaker(clock=lambda: t[0])
    caplog.set_level(logging.WARNING, logger="selection")
    for _ in range(W.HB_FAIL_THRESHOLD - 1): br.heartbeat_result(SSL_ERR)
    assert not br.is_open()
    br.heartbeat_result(SSL_ERR); assert br.is_open() and br.held
    t[0] = 3600; assert br.is_open()                  # 不按时间自动恢复：网络没好就一直暂停
    br.heartbeat_result(None); assert not br.is_open() and not br.held
    txt = caplog.text
    assert "network self-check" in txt and "pausing new claims" in txt and "recovered" in txt


def test_heartbeat_non_network_error_does_not_hold():
    br = W.NetBreaker(clock=lambda: 0)
    for _ in range(10): br.heartbeat_result(urllib.error.HTTPError("u", 401, "unauthorized", {}, None))
    assert not br.is_open()


def test_heartbeat_success_resets_streak():
    br = W.NetBreaker(clock=lambda: 0)
    br.heartbeat_result(SSL_ERR); br.heartbeat_result(SSL_ERR); br.heartbeat_result(None); br.heartbeat_result(SSL_ERR)
    assert not br.is_open()


def test_hold_and_cooldown_independent():
    t = [0.0]; br = W.NetBreaker(clock=lambda: t[0])
    for _ in range(3): br.fail("goto")
    for _ in range(W.HB_FAIL_THRESHOLD): br.heartbeat_result(SSL_ERR)
    t[0] = 1000; assert br.is_open()                  # 冷却已过但心跳仍失败 → 仍暂停
    br.heartbeat_result(None); assert not br.is_open()


def test_heartbeat_loop_feeds_breaker(monkeypatch):
    br = W.NetBreaker(clock=lambda: 0); monkeypatch.setattr(W, "NET", br)
    n = [0]
    class Stop(BaseException): pass
    class St:
        def heartbeat(s, info):
            n[0] += 1
            if n[0] > W.HB_FAIL_THRESHOLD: raise Stop()
            raise SSL_ERR
    monkeypatch.setattr(W, "RUN_STALE_CHECK_EVERY", 10**9)
    with pytest.raises(Stop): W._heartbeat_loop(St(), every=0, sleep=lambda s: None)
    assert br.held


def test_main_loop_skips_claim_while_held(monkeypatch):
    br = W.NetBreaker(clock=lambda: 0)
    for _ in range(W.HB_FAIL_THRESHOLD): br.heartbeat_result(SSL_ERR)
    monkeypatch.setattr(W, "NET", br); monkeypatch.setattr(W, "LOOKUPS_ON", False); monkeypatch.setattr(W, "KEEPALIVE_ON", False)
    claims, sl = [], []
    class Stop(BaseException): pass
    class St:
        def purge(s): return {}
        def lookup_purge(s): return 0
        def exclude_words(s): return []
        def claim(s): claims.append(1); raise Stop()
    monkeypatch.setattr(W, "Store", St); monkeypatch.setattr(W, "purge_local", lambda: 0)
    def fake_sleep(s):
        sl.append(s)
        if len(sl) == 5: br.heartbeat_result(None)   # 心跳恢复
    monkeypatch.setattr(W.time, "sleep", fake_sleep)
    with pytest.raises(Stop): W.main(["--fake", "x.json"])
    assert len(sl) >= 5 and claims == [1]


# ---- 文案精确：只有“打不开页面/断网/SSL/数据库连不上”才说 box 网络异常；点按钮超时、浏览器占用另说 ----
@pytest.mark.parametrize("e,label", [
    (GOTO, "box 网络异常"),
    (RuntimeError("Page.goto: net::ERR_CONNECTION_RESET at https://ehunt.ai/"), "box 网络异常"),
    (SSL_ERR, "box 网络异常"),
    (W.PageNotReady("EHunt 页面 45 秒内没有出现搜索框/结果列表"), "box 网络异常"),
    (ConnectionResetError(104, "Connection reset by peer"), "box 网络异常"),
    (PwTimeout("Page.click: Timeout 30000ms exceeded."), "EHunt 页面响应超时"),
    (W.PageTimeout("EHunt 第 1 页读取超时（每次 90 秒，已重试 2 次仍失败）"), "EHunt 页面响应超时"),
    (RuntimeError("Failed to create a ProcessSingleton for your profile directory"), "box 抓取浏览器被占用"),
])
def test_net_label(e, label):
    assert W.net_label(e) == label


def test_count_click_timeout_final_message_not_network(monkeypatch, nsleep):
    calls = _count(monkeypatch, 3, PwTimeout("Page.click: Timeout 30000ms exceeded."))
    (_, _, kw), = [c for c in calls if c[0] == "if"]
    assert kw["error"].startswith("EHunt 页面响应超时") and "box 网络异常" not in kw["error"]


def test_net_label_db_http_error():
    assert W.net_label(urllib.error.HTTPError("u", 503, "x", {}, None)) == "数据库暂时不可用（HTTP 503）"
