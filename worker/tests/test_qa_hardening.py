"""QA 报告 P0/P1 修复的单元测试。"""
import json
from datetime import timedelta
from unittest import mock

from analyzer.cache import MemoryCache, StoreCache
from analyzer.config import Config
from analyzer.errors import PermanentError, is_retryable
from analyzer.etsy_url import parse_listing_id, is_short, resolve_short
from analyzer.http import HttpError
from analyzer.main import run_loop
from analyzer.pipeline import process_job
from analyzer.providers import build_providers, ehunt, keepamore
from analyzer.providers.ehunt import EHuntEtsy, EHuntQuotaError, NOT_FOUND
from analyzer.providers.keepamore import KeepamoreAmazon
from analyzer.queue import MemoryStore, _now, next_utc_midnight

OTHER = {"code": 200, "quota": {"used_today": 8, "remaining_today": 192}, "data": {"list": [{
    "title": "Custom Pet Wedding Cocktail Napkins", "product_url": "https://www.etsy.com/listing/1234567/napkins"}]}}
GOOD = {"code": 200, "quota": {"used_today": 8, "remaining_today": 192}, "data": {"list": [
    {"title": "Other", "product_url": "https://www.etsy.com/listing/420/x"},
    {"title": "Boo Tag", "price": "9", "tags": "a,b", "reviews": 3, "product_url": "https://www.etsy.com/listing/42/boo"}]}}


def mock_providers():
    return build_providers(Config())


# ---- P0-1 不回退到第一条 ----
def test_ehunt_no_fallback_to_first_result():
    with mock.patch.object(ehunt, "request_json", return_value=OTHER):
        try:
            EHuntEtsy("k").get_listing(1)
            assert False
        except PermanentError as e:
            assert NOT_FOUND in str(e)


def test_ehunt_exact_id_match_not_prefix():
    with mock.patch.object(ehunt, "request_json", return_value=GOOD):
        p = EHuntEtsy("k").get_listing(42)
    assert p.title == "Boo Tag"  # 不会把 listing/420 当成 42


def test_not_found_fails_once_without_retry():
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/1/qa")
    _, amazon, llm = mock_providers()
    with mock.patch.object(ehunt, "request_json", return_value=OTHER) as rq:
        e = EHuntEtsy("k", cache=MemoryCache())
        run_loop(store, (e, amazon, llm), Config(), once=True)
    j = store.jobs[jid]
    assert j["status"] == "failed" and NOT_FOUND in j["error"] and j["attempts"] == 1 and rq.call_count == 1
    assert jid not in store.analyses


# ---- P0-2 缓存 ----
def test_ehunt_cache_hit_spends_zero():
    c = MemoryCache()
    with mock.patch.object(ehunt, "request_json", return_value=GOOD) as rq:
        e = EHuntEtsy("k", cache=c)
        e.get_listing(42)
        p = e.get_listing(42)
        try:
            e.get_listing(1)
        except PermanentError:
            pass
        try:
            e.get_listing(1)  # 负缓存
        except PermanentError as ex:
            assert "缓存" in str(ex)
    assert rq.call_count == 2 and e.cache_hits == 2 and p.raw["cache"] and p.title == "Boo Tag"
    assert e.fetch_listing_reviews(42, 100, 0) == ([], 3)


def _km(path_resp):
    def f(method, url, headers=None, **k):
        for key, resp in path_resp.items():
            if key in url:
                return resp
        raise AssertionError(url)
    return f


def test_keepamore_cache_search_and_products():
    sr = {"code": "0000", "data": {"products": [{"asin": "A1"}, {"asin": "A2"}]}, "metadata": {"unitsConsumed": 5}}
    pr = {"code": "0000", "data": {"products": [{"asin": "A1", "title": "t1"}, {"asin": "A2", "title": "t2"}]},
          "metadata": {"unitsConsumed": 2}}
    c = MemoryCache()
    with mock.patch.object(keepamore, "request_json", side_effect=_km({"/productsearch": sr, "/product?": pr})) as rq:
        k = KeepamoreAmazon("km_x", cache=c)
        a = k.search("Boo Tag")
        b = k.search("boo tag ")
    assert rq.call_count == 2 and k.units_used == 7 and k.cache_hits == 3
    assert [i.asin for i in a.items] == [i.asin for i in b.items] == ["A1", "A2"] and b.total_results == 2


def test_store_cache_swallows_errors():
    class Bad:
        def cache_get_many(self, k): raise RuntimeError()
        def cache_put(self, *a): raise RuntimeError()
    c = StoreCache(Bad())
    assert c.get("x") is None
    c.put("x", "p", {}, 1)


# ---- P0-3 熔断/低额度停止领取 ----
def test_low_remaining_stops_claiming():
    store = MemoryStore()
    j1 = store.enqueue("https://www.etsy.com/listing/42/a")
    j2 = store.enqueue("https://www.etsy.com/listing/43/b")
    _, amazon, llm = mock_providers()
    low = json.loads(json.dumps(GOOD)); low["quota"]["remaining_today"] = 0
    with mock.patch.object(ehunt, "request_json", return_value=low):
        run_loop(store, (EHuntEtsy("k"), amazon, llm), Config(), once=True)
    assert store.jobs[j1]["status"] == "succeeded" and store.jobs[j2]["status"] == "queued"
    assert store.status["ehunt_paused_until"] == next_utc_midnight() and store.status["ehunt_remaining"] == 0


# ---- P1-4 错误分类 ----
def test_retryable_classes():
    assert not is_retryable(PermanentError("x"))
    assert not is_retryable(HttpError("HTTP 404 POST x"))
    assert not is_retryable(HttpError("EHunt 错误 500: bad"))
    assert is_retryable(HttpError("HTTP 503 GET x")) and is_retryable(HttpError("HTTP 429 GET x"))
    assert is_retryable(TimeoutError()) and is_retryable(HttpError("<urlopen error timed out>"))
    assert is_retryable(json.JSONDecodeError("x", "", 0), 1) and not is_retryable(json.JSONDecodeError("x", "", 0), 2)


# ---- P1-5 429 熔断 ----
def test_429_circuit_breaker_keeps_queue():
    store = MemoryStore()
    j1 = store.enqueue("https://www.etsy.com/listing/42/a")
    j2 = store.enqueue("https://www.etsy.com/listing/43/b")
    _, amazon, llm = mock_providers()
    with mock.patch.object(ehunt, "request_json", side_effect=HttpError("HTTP 429 POST x")) as rq:
        run_loop(store, (EHuntEtsy("k"), amazon, llm), Config(), once=True)
    assert rq.call_count == 1
    for j in (j1, j2):
        assert store.jobs[j]["status"] == "queued"
    assert store.jobs[j1]["attempts"] == 0 and store.jobs[j1]["run_after"] == next_utc_midnight()
    assert store.status["ehunt_paused_until"] > _now()


def test_401_is_not_breaker():
    with mock.patch.object(ehunt, "request_json", side_effect=HttpError("HTTP 401 POST x")):
        try:
            EHuntEtsy("k").get_listing(1)
        except EHuntQuotaError as e:
            assert not e.breaker


# ---- P1-7 取消运行中任务 ----
def test_cancel_between_stages():
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/42/a")
    etsy, amazon, llm = mock_providers()
    job = store.claim_next_job("w")
    orig = llm.analyze_product
    def analyze(*a):
        store.jobs[jid]["cancel_requested"] = True
        return orig(*a)
    llm.analyze_product = analyze
    process_job(job, store, etsy, amazon, llm)
    j = store.jobs[jid]
    assert j["status"] == "canceled" and j["stage"] == "analyze" and jid not in store.analyses


def test_stage_updates_heartbeat():
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/42/a")
    run_loop(store, mock_providers(), Config(), once=True)
    assert store.jobs[jid]["status"] == "succeeded" and store.jobs[jid]["locked_at"] is not None


# ---- P1-9 链接 ----
def test_url_variants():
    assert parse_listing_id("https://m.etsy.com/listing/123/x") == 123
    assert parse_listing_id("etsy.com/uk/listing/5") == 5
    assert parse_listing_id("https://www.notetsy.com/listing/5") is None
    assert is_short("https://etsy.me/3AbCd") and not is_short("https://www.etsy.com/listing/1")


def test_resolve_short_reads_location():
    class R:
        def __init__(self, loc): self.headers = {"Location": loc}
    class Op:
        def __init__(self): self.n = 0
        def open(self, req, timeout=0):
            self.n += 1
            return R("https://www.etsy.com/listing/777/thing?utm=1")
    assert resolve_short("https://etsy.me/abc", opener=Op()) == "https://www.etsy.com/listing/777/thing?utm=1"


def test_usage_logged_per_provider():
    store = MemoryStore()
    store.enqueue("https://www.etsy.com/listing/42/a")
    _, amazon, llm = mock_providers()
    with mock.patch.object(ehunt, "request_json", return_value=GOOD):
        run_loop(store, (EHuntEtsy("k"), amazon, llm), Config(), once=True)
    assert store.usage["ehunt"] == [1, 1, 0] and "keepamore" not in store.usage  # mock 不计


def test_next_utc_midnight():
    n = _now()
    m = next_utc_midnight(n)
    assert m > n and m - n <= timedelta(days=1) and m.hour == 0
