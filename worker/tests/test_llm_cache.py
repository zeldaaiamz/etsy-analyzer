from analyzer.cache import MemoryCache
from analyzer.models import AmazonItem, EtsyProduct
from analyzer.providers import llm_openai
from analyzer.providers.llm_openai import OpenAICompatLLM

ID = {"product_summary": "杯子", "category": "Home", "target_audience": "人", "keywords": ["coffee mug"],
      "keyword_reasoning": "r"}


def mk(cache):
    l = OpenAICompatLLM("http://x", "k", "m", cache=cache)
    l.n = 0

    def chat(s, u):
        l.n += 1
        if s == llm_openai.MATCH_PROMPT:
            return {"results": [{"i": c["i"], "label": "同款" if "Mug" in c["title"] else "无关", "reason": "x"}
                                for c in u["candidates"]]}
        return ID
    l._chat = chat
    return l


def test_identify_cached_per_listing():
    c = MemoryCache()
    a, b = mk(c), mk(c)
    r1 = a.analyze_product(EtsyProduct(7, "Mug"), [])
    r2 = b.analyze_product(EtsyProduct(7, "Mug"), [])
    assert a.n == 1 and b.n == 0 and b.cache_hits == 1
    assert r2.keywords == r1.keywords == ["coffee mug"] and r2.product_summary == "杯子"
    b.analyze_product(EtsyProduct(8, "Mug"), [])
    assert b.n == 1  # other listing misses


def test_prompt_change_invalidates(monkeypatch):
    c = MemoryCache()
    mk(c).analyze_product(EtsyProduct(7, "Mug"), [])
    monkeypatch.setattr(llm_openai, "REDUCE_PROMPT", llm_openai.REDUCE_PROMPT + "x")
    b = mk(c)
    b.analyze_product(EtsyProduct(7, "Mug"), [])
    assert b.n == 1


def test_match_cached_per_asin_set_order_independent():
    c = MemoryCache()
    a, b = mk(c), mk(c)
    p = EtsyProduct(7, "Mug")
    a.rank_similarity(p, [AmazonItem("A", "Mug"), AmazonItem("B", "Sock")])
    out = b.rank_similarity(p, [AmazonItem("B", "Sock"), AmazonItem("A", "Mug")])
    assert a.n == 1 and b.n == 0 and [i.asin for i in out] == ["A", "B"] and out[0].similarity == 0.9
    b.rank_similarity(p, [AmazonItem("A", "Mug"), AmazonItem("C", "Cup")])
    assert b.n == 1  # different ASIN set misses


def test_match_failure_not_cached():
    c = MemoryCache()
    a = mk(c)
    a._chat = lambda s, u: (_ for _ in ()).throw(RuntimeError("boom"))
    a.rank_similarity(EtsyProduct(7, "Mug"), [AmazonItem("A", "Mug")])
    assert not c.d
