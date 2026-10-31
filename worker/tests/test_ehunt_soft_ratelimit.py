"""EHunt 软限流：横幅 / 未筛选 Find → RateLimited；不烧关键词重试；KeywordNotApplied 在无限流时仍成立。"""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402
import test_sel_page_timeout as T  # noqa: E402  同目录假 session

BANNER = "Unusually high request volume detected. Access is temporarily limited. Try again later."


def test_page_shows_rate_limit_banner():
    assert W.page_shows_rate_limit(BANNER)
    assert W.page_shows_rate_limit(f"x\n{BANNER}\nFind 85,757,172 products")
    assert W.page_shows_rate_limit("请求量异常，暂时限制访问")
    assert not W.page_shows_rate_limit("")
    assert not W.page_shows_rate_limit("Find 85,757,172 products, show you the first 5,000")
    assert not W.page_shows_rate_limit("Try again later")  # 单独太泛，不认


def test_unfiltered_after_search_heuristic():
    assert W.unfiltered_after_search(85_757_172, False)
    assert W.unfiltered_after_search(W.UNFILTERED_FIND_MIN, False)
    assert not W.unfiltered_after_search(85_757_172, True)   # 有 Keyword chip → 不是限流误判
    assert not W.unfiltered_after_search(296, False)         # 真·关键词未生效（小结果）
    assert not W.unfiltered_after_search(0, False)
    assert not W.unfiltered_after_search(None, False)
    assert not W.unfiltered_after_search("x", False)


def test_rate_limit_msg_is_chinese_and_distinct():
    assert "限流" in W.RATE_LIMIT_MSG and "请求过多" in W.RATE_LIMIT_MSG
    assert "关键词未生效" not in W.RATE_LIMIT_MSG
    assert W.RateLimited is not W.KeywordNotApplied


def test_rate_limited_not_in_retry_kinds():
    assert W.RateLimited not in W.RETRY_KINDS
    assert W.KeywordNotApplied in W.RETRY_KINDS


def test_page_loop_rate_limited_fails_fast_no_retries():
    c = T.Clock()
    s = T.OpenFailSite(c, open_fail=99, exc=W.RateLimited(W.RATE_LIMIT_MSG))
    with pytest.raises(W.RateLimited, match="限流"):
        T.run(s, c)
    assert s.opens == [1]


def test_page_loop_keyword_still_retried_when_not_rate_limited():
    c = T.Clock()
    s = T.OpenFailSite(c, open_fail=2, exc=W.KeywordNotApplied("关键词未生效：Selected: 中没有 Keyword: book"), total=1000)
    rows = T.run(s, c, start=870, end=900)
    assert [r["rank"] for r in rows] == list(range(870, 901)) and s.opens == [44, 44, 44]


def test_handle_run_rate_limited_surfaces_msg(monkeypatch, tmp_path):
    def scrape(kw, params, s0, e0, between=None, should_stop=None):
        c = T.Clock()
        site = T.OpenFailSite(c, open_fail=99, exc=W.RateLimited(W.RATE_LIMIT_MSG))
        yield from W.page_loop(site.open, s0, e0, T.PS, timeout=90, retries=2, clock=c, watchdog=lambda x: T.NoDog(), should_stop=should_stop)

    rp, bp = T._handle(monkeypatch, tmp_path, scrape, ["running"])
    err = [k for k in rp if k.get("status") == "canceled"][-1]["error"]
    assert "限流" in err and "请求过多" in err
    assert "关键词未生效" not in err


class _FakeLoc:
    def __init__(self, text): self._t = text
    @property
    def first(self): return self
    def inner_text(self, timeout=None): return self._t


class _FakePg:
    def __init__(self, body, selected="Selected:"):
        self._body, self._sel = body, selected
    def locator(self, sel):
        if sel == "body": return _FakeLoc(self._body)
        if "Selected" in sel: return _FakeLoc(self._sel)
        return _FakeLoc("")


def test_check_rate_limit_banner_raises():
    with pytest.raises(W.RateLimited, match="限流"):
        W._check_rate_limit(_FakePg(BANNER), W.ui())


def test_check_rate_limit_unfiltered_find_raises():
    body = "Find 85,757,172 products, show you the first 5,000"
    with pytest.raises(W.RateLimited, match="限流"):
        W._check_rate_limit(_FakePg(body, "Selected:"), W.ui(), keyword="squishy", after_search=True)


def test_check_rate_limit_real_keyword_miss_no_raise():
    """无横幅、Find 很小、无 Keyword chip → 不抛 RateLimited（留给 KeywordNotApplied）。"""
    W._check_rate_limit(_FakePg("Find 0 products", "Selected:"), W.ui(), keyword="book", after_search=True)


def test_check_rate_limit_applied_keyword_skips_unfiltered_heuristic():
    W._check_rate_limit(
        _FakePg("Find 85,757,172 products", "Selected: Keyword: squishy"),
        W.ui(), keyword="squishy", after_search=True,
    )
