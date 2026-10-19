"""EHunt 网页回退（033 + ETSY_PROVIDER=ehunt_web）：box 只读主图；ECS provider 缓存/等待/熔断；预筛选与分析段容忍无标题/价格。"""
import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest

from analyzer import queue, pipeline
from analyzer.cache import StoreCache, MemoryCache
from analyzer.config import Config
from analyzer.errors import PermanentError
from analyzer.providers import build_providers
from analyzer.providers.ehunt import EHuntServiceError, EHuntQuotaError
from analyzer.providers.ehunt_web import EHuntWebEtsy, EHuntWebPending
from analyzer.queue import MemoryStore, _now
from tests.test_quota_resume import Clock, T0

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402

IMG = "https://i.etsystatic.com/12905279/r/il/ec7387/3390356664/il_570xN.3390356664_cj3w.jpg"


# ---------------- box 端：只读主图、精确匹配 ----------------
def test_match_image_exact_listing_only():
    rows = [("https://www.etsy.com/listing/10849851341/x", "https://i.etsystatic.com/1/r/il/a/2/il_340x270.2_x.jpg"),
            ("https://www.etsy.com/listing/1084985134/y?ref=ehunt", "https://i.etsystatic.com/1/r/il/a/3/il_340x270.3_y.jpg")]
    assert W.match_image(rows, 1084985134) == "https://i.etsystatic.com/1/r/il/a/3/il_570xN.3_y.jpg"
    assert W.match_image(rows[:1], 1084985134) is None                       # 前缀相同的别的商品不算
    assert W.match_image([("https://www.etsy.com/listing/5", "data:image/png;base64,AAA")], 5) == ""   # 占位图：找到但无主图
    assert W.match_image([], 5) is None


class FakeBoxStore:
    def __init__(self, ids): self.q, self.patches = [{"listing_id": i} for i in ids], {}
    def lookup_claim(self, n): out, self.q = self.q[:n], self.q[n:]; return out
    def lookup_patch(self, lid, **kw): self.patches[lid] = kw


def test_serve_lookups_done_notfound_error_login():
    st = FakeBoxStore([1, 2, 3, 4, 5, 6])
    opened = []
    def factory():
        opened.append(1)
        def f(lid):
            if lid == 2: return None
            if lid == 3: raise RuntimeError("page broke")
            if lid == 5: raise W.LoginExpired("login gone")
            return IMG
        return f
    n = W.serve_lookups(st, factory, limit=10)
    p = st.patches
    assert p[1]["status"] == "done" and p[1]["image_url"] == IMG and set(p[1]) == {"status", "image_url", "error", "finished_at"}
    assert p[2]["status"] == "not_found" and p[2]["image_url"] is None
    assert p[3]["status"] == "error" and "page broke" in p[3]["error"]
    assert p[4]["status"] == "done" and len(opened) == 2                     # 出错后重开页面
    assert p[5]["status"] == p[6]["status"] == "login_expired" and p[6]["error"].startswith(W.LOGIN_TAG)
    assert n == 4


def test_serve_lookups_no_requests_never_opens_browser():
    st = FakeBoxStore([])
    assert W.serve_lookups(st, lambda: pytest.fail("browser opened")) == 0


# ---------------- ECS 端 provider ----------------
class LookupStore(MemoryStore):
    """模拟 033：lookup_request 返回 box 已处理好的结果（或排队中）。"""
    def __init__(self, results=None):
        super().__init__(); self.results, self.req = dict(results or {}), []
    def lookup_request(self, lid):
        self.req.append(lid)
        r = self.results.get(lid, {"status": "queued", "requested_at": _now().isoformat()})
        return {"listing_id": lid, **r}
    def lookup_get(self, lid): return self.lookup_request(lid)


def prov(store, **kw):
    return EHuntWebEtsy(store, cache=StoreCache(store), sleep=lambda s: None, **kw)


def test_web_done_image_only_and_cached():
    st = LookupStore({7: {"status": "done", "image_url": IMG}})
    st.status = {"ehunt_paused_until": _now() + timedelta(minutes=20), "reason": "EHunt 服务异常（查询失败: 'daily_limit'）"}
    e = prov(st)
    p = e.get_listing(7)
    assert (p.title, p.price, p.tags, p.image_url, p.raw["image_only"]) == ("", None, [], IMG, True)
    assert st.status.get("ehunt_paused_until") is None and st.status.get("reason") is None   # 网页成功 → 解除旧 API 熔断
    p2 = e.get_listing(7)
    assert p2.image_url == IMG and p2.raw.get("cache") and st.req == [7] and e.cache_hits == 1
    assert e.calls == 0 and e.web_lookups == 1                                 # 不计入 EHunt API 用量


def test_web_reuses_api_cache_first():
    st = LookupStore()
    st.cache_put("ehunt:listing:9", "ehunt", {"listing_id": 9, "title": "API title", "description": "", "price": 12.5, "image_url": IMG,
                                              "raw": {"provider": "ehunt", "ehunt": {"reviews": 3}}}, 3600)
    p = prov(st).get_listing(9)
    assert p.title == "API title" and st.req == []


def test_web_not_found_permanent_and_cached():
    st = LookupStore({8: {"status": "not_found"}})
    e = prov(st)
    with pytest.raises(PermanentError): e.get_listing(8)
    with pytest.raises(PermanentError): e.get_listing(8)
    assert st.req == [8]


def test_web_login_expired_is_service_anomaly():
    st = LookupStore({8: {"status": "login_expired", "error": "x"}})
    with pytest.raises(EHuntServiceError) as ei: prov(st).get_listing(8)
    assert ei.value.service_anomaly and "EHunt 未登录" in str(ei.value)


def test_web_pending_requeues_and_stale_box_is_anomaly():
    st = LookupStore()
    with pytest.raises(EHuntWebPending) as ei: prov(st, timeout=0).get_listing(5)
    assert ei.value.rate_limited
    st.results[6] = {"status": "queued", "requested_at": (_now() - timedelta(minutes=30)).isoformat()}
    with pytest.raises(EHuntServiceError, match="box 取数 worker 未响应"): prov(st, timeout=0).get_listing(6)


def test_prefer_api_falls_back_to_web():
    st = LookupStore({4: {"status": "done", "image_url": IMG}})
    api = mock.Mock(calls=0, used_today=None, remaining=None, _totals={})
    api.get_listing.side_effect = EHuntServiceError("EHunt 服务异常（查询失败: 'daily_limit'）")
    assert prov(st, api=api).get_listing(4).image_url == IMG
    api.get_listing.side_effect = EHuntQuotaError("quota", breaker=True)
    st.kv.clear()
    assert prov(st, api=api).get_listing(4).image_url == IMG


def test_factory_builds_ehunt_web():
    st = LookupStore()
    e, _, _ = build_providers(Config(etsy_provider="ehunt_web"), cache=StoreCache(st), store=st)
    assert isinstance(e, EHuntWebEtsy) and e.api is None
    e, _, _ = build_providers(Config(etsy_provider="ehunt_web", ehunt_api_key="k", ehunt_web_prefer_api=True), cache=StoreCache(st), store=st)
    assert e.api is not None


# ---------------- 预筛选 + 分析段：无标题/价格也能跑通 ----------------
class VisionLLM:
    """模拟真实 LLM：无标题时靠看图得到产品与搜索词（kws=[] 模拟看图失败）。"""
    def __init__(self, inner, kws=("macrame wall hanging",)): self.inner, self.kws = inner, list(kws)
    def analyze_product(self, p, reviews):
        an = self.inner.analyze_product(p, reviews)
        assert p.title == "" and p.image_url == IMG
        an.keywords, an.keyword_items, an.product_summary = list(self.kws), [], "macrame wall hanging (from image)"
        return an
    def __getattr__(self, k): return getattr(self.inner, k)


def _run(store, etsy, clock, kws=None):
    from analyzer.main import run_loop
    _, amazon, llm = build_providers(Config())
    if kws is not None: llm = VisionLLM(llm, kws)
    with mock.patch.object(queue, "_now", clock), mock.patch.object(pipeline, "_now", clock):
        run_loop(store, (etsy, amazon, llm), Config(), once=True)


def _setup(results, n=2):
    st = LookupStore(results)
    ids = [st.enqueue(f"https://www.etsy.com/listing/{100 + i}/x", prescreen=True) for i in range(n)]
    for i, jid in enumerate(ids):
        st.jobs[jid].update(run_after=T0 - timedelta(minutes=10), queued_at=T0 - timedelta(minutes=10), created_at=T0 - timedelta(minutes=10, seconds=-i))
    return st, ids


def test_prescreen_image_only_then_analysis_tolerates_missing_title():
    st, ids = _setup({100: {"status": "done", "image_url": IMG}, 101: {"status": "done", "image_url": IMG}})
    clock = Clock(T0)
    e = prov(st)
    _run(st, e, clock)
    for i in ids:
        assert (st.jobs[i]["status"], st.jobs[i]["screen_state"]) == ("screening", "awaiting")
        assert st.etsy[i]["image_url"] == IMG and st.etsy[i]["title"] == "" and st.etsy[i]["price"] is None
    with mock.patch.object(queue, "_now", clock):
        st.screen_confirm(ids, [])
    _run(st, e, clock, kws=["macrame wall hanging"])
    for i in ids:
        assert st.jobs[i]["status"] == "succeeded", st.jobs[i].get("error")
        tr = st.analyses[i]["decision_trace"]
        assert "仅依据 Etsy 主图" in tr[0]["reasoning"]


def test_prescreen_login_expired_requeues_without_attempt_cost():
    st, ids = _setup({100: {"status": "login_expired"}}, n=1)
    _run(st, prov(st), Clock(T0))
    j = st.jobs[ids[0]]
    assert j["status"] == "queued" and j["attempts"] == 0 and "EHunt 未登录" in j["error"]
    assert st.status.get("ehunt_paused_until") and "EHunt 未登录" in st.status.get("reason")


def test_image_only_without_keywords_fails_clearly_instead_of_empty_search():
    st, ids = _setup({100: {"status": "done", "image_url": IMG}}, n=1)
    clock = Clock(T0); e = prov(st)
    _run(st, e, clock)
    with mock.patch.object(queue, "_now", clock):
        st.screen_confirm(ids, [])
    _run(st, e, clock, kws=[])
    j = st.jobs[ids[0]]
    assert j["status"] == "failed" and "只取到主图" in j["error"] and not st.matches
