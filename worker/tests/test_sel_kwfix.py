"""2026-11-18 Hongyan 批准：搜索后关键词标签轮询最多 15 秒；前两次没生效直接重搜，第 3 次才按「未筛选巨量」判限流；
Chrome 启动超时 → 「box 抓取浏览器启动超时（box 太忙）」。模拟页面，不连真实 EHunt。"""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402


class PwTimeout(Exception): pass
PwTimeout.__name__ = "TimeoutError"


class Txt:
    def __init__(self, f): self.f = f
    @property
    def first(self): return self
    def inner_text(self, timeout=None): return self.f()
    def count(self): return 0
    def nth(self, i): return self
    def is_visible(self): return False


class KwPage:
    """searches：第 k 次搜索后 chip 在 appear_after[k] 秒出现（None = 不出现）；Find 结果数固定 find。"""
    def __init__(self, U, clock, appear_after, kw, find=85_000_000, banner=False):
        self.U, self.clock, self.appear, self.kw, self.find, self.banner = U, clock, list(appear_after), kw, find, banner
        self.searches = 0; self.t_search = None
    def fill(self, sel, v): self.v = v
    def _chip(self):
        if not self.searches: return False
        a = self.appear[min(self.searches, len(self.appear)) - 1]
        return a is not None and self.clock() - self.t_search >= a
    def locator(self, sel):
        if sel == self.U["selected_row"]: return Txt(lambda: f"Selected: Keyword: {self.kw}" if self._chip() else "Selected:")
        if sel == "body": return Txt(lambda: ("Too many requests " if self.banner else "") + f"Find {self.find:,} products")
        return Txt(lambda: "")
    def query_selector(self, s): return None
    def wait_for_load_state(self, *a, **k): pass


@pytest.fixture
def env(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(W, "_ready_clock", lambda: t[0])
    monkeypatch.setattr(W, "_ready_sleep", lambda s: t.__setitem__(0, t[0] + s))
    monkeypatch.setattr(W.time, "sleep", lambda s: t.__setitem__(0, t[0] + s))
    monkeypatch.setattr(W, "dismiss_overlays", lambda *a, **k: [])
    def click(pg, U, target, **k):
        pg.searches += 1; pg.t_search = t[0]
    monkeypatch.setattr(W, "_click", click)
    monkeypatch.setattr(W, "page_shows_rate_limit", lambda body: "Too many requests" in (body or ""))
    return t


def test_constants():
    assert W.KW_CHIP_WAIT_S == 15 and W.KW_ATTEMPTS == 3


def test_chip_slow_but_within_15s_no_research(env):
    U = W.ui(); pg = KwPage(U, lambda: env[0], [12], "tie")
    assert W.apply_keyword(pg, U, "tie") == 1 and pg.searches == 1


def test_first_two_miss_then_third_ok_not_rate_limited(env):
    U = W.ui(); pg = KwPage(U, lambda: env[0], [None, None, 1], "tie")   # Find 一直是未筛选巨量，但前两次不判限流
    assert W.apply_keyword(pg, U, "tie") == 3 and pg.searches == 3


def test_three_misses_with_unfiltered_find_is_rate_limited(env):
    U = W.ui(); pg = KwPage(U, lambda: env[0], [None], "tie")
    with pytest.raises(W.RateLimited): W.apply_keyword(pg, U, "tie")
    assert pg.searches == 3


def test_three_misses_small_find_is_keyword_not_applied(env):
    U = W.ui(); pg = KwPage(U, lambda: env[0], [None], "tie", find=50)
    with pytest.raises(W.KeywordNotApplied): W.apply_keyword(pg, U, "tie")
    assert pg.searches == 3


def test_explicit_rate_limit_banner_still_stops_immediately(env):
    """EHunt 页面明确写了限流（横幅）→ 不再重搜（重搜只会加重限流）；这和「关键词没生效」的推断不同。"""
    U = W.ui(); pg = KwPage(U, lambda: env[0], [None], "tie", banner=True)
    with pytest.raises(W.RateLimited): W.apply_keyword(pg, U, "tie")
    assert pg.searches == 1


def test_each_attempt_waits_full_15s(env):
    U = W.ui(); pg = KwPage(U, lambda: env[0], [None, 0.5], "tie")
    assert W.apply_keyword(pg, U, "tie") == 2
    assert env[0] >= 15


LAUNCH_TO = PwTimeout("BrowserType.launch_persistent_context: Timeout 180000ms exceeded.\nCall log:\n  - <launching> /opt/google/chrome/chrome --")


def test_launch_timeout_label():
    assert W.is_net_error(LAUNCH_TO)
    assert W.net_label(LAUNCH_TO) == "box 抓取浏览器启动超时（box 太忙）"


def test_count_launch_timeout_final_message(monkeypatch):
    monkeypatch.setattr(W, "count_ehunt", lambda kw, p: (_ for _ in ()).throw(LAUNCH_TO))
    monkeypatch.setattr(W, "NET", W.NetBreaker(clock=lambda: 0))
    calls = []
    class St:
        def patch_run_if(s, rid, expect, **kw): calls.append(kw)
        def patch_run(s, rid, **kw): pass
    class A: fake = None
    W.handle_run(St(), {"id": "r", "keyword": "basket", "status": "counting", "attempts": 3, "params": {}}, A(), False)
    assert calls[-1]["error"].startswith("box 抓取浏览器启动超时（box 太忙）")
