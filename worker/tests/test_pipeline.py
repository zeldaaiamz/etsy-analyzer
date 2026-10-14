from analyzer.config import Config
from analyzer.etsy_url import parse_listing_id
from analyzer.providers import build_providers
from analyzer.queue import MemoryStore
from analyzer.main import run_loop


def test_parse_listing_id():
    assert parse_listing_id("https://www.etsy.com/listing/1234567890/foo?ref=x") == 1234567890
    assert parse_listing_id("https://www.etsy.com/de/listing/42/foo") == 42
    assert parse_listing_id("https://example.com/listing/1") is None


def test_end_to_end_mock():
    cfg = Config()
    store = MemoryStore()
    good = store.enqueue("https://www.etsy.com/listing/1234567890/line-art")
    bad = store.enqueue("https://www.etsy.com/shop/Foo")
    run_loop(store, build_providers(cfg), cfg, once=True)
    assert store.jobs[good]["status"] == "succeeded"
    assert store.jobs[bad]["status"] == "failed"
    an = store.analyses[good]
    assert an["keywords"] and 0 <= an["opportunity_score"] <= 100
    assert an["competition_level"] in ("low", "medium", "high")
    assert any(m["job_id"] == good for m in store.matches)
    ev = an["audience_evidence"]
    assert ev["source"] == "reviews" and ev["reviews_analyzed"] >= 3 and ev["evidence"]
    steps = [s["title"] for s in an["decision_trace"]]
    assert steps == ["识别产品", "判断人群", "生成搜索词", "亚马逊搜索", "匹配同款/类似", "竞争与销量评估", "结论"]
    kinds = {s["kind"] for s in store.snapshots}
    assert "search_result" in kinds and kinds & {"identical", "similar"}
    assert all(s["image_path"] in store.files for s in store.snapshots)
    assert all(s["image_path"].startswith("mock-user/") for s in store.snapshots)


def test_audience_from_listing_text_when_no_reviews():
    from analyzer.audience import infer_audience, audience_summary
    from analyzer.models import EtsyProduct
    p = EtsyProduct(listing_id=1, title="Halloween Teacher Gift Tag", tags=["halloween", "teacher gift"])
    aud, ev = infer_audience(p, [])
    assert ev["source"] == "listing_text" and ev["reviews_analyzed"] == 0
    assert "无评论" in audience_summary(ev, aud)


def test_retry_then_fail():
    cfg = Config()
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/1/x")
    etsy, amazon, llm = build_providers(cfg)

    class Boom:
        def get_listing(self, _):
            raise RuntimeError("api down")
    from analyzer.pipeline import process_job
    from analyzer.queue import _now
    for _ in range(3):
        store.jobs[jid]["run_after"] = _now()
        job = store.claim_next_job("t")
        process_job(job, store, Boom(), amazon, llm)
    assert jid not in store.jobs  # 第 3 次失败后自动删除


def test_retry_exhausted_library_job_marked_failed():
    cfg = Config()
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/1/x")
    store.jobs[jid]["etsy_listing_id"] = "1"
    store.library.add(("mock-user", "1"))
    etsy, amazon, llm = build_providers(cfg)

    class Boom:
        def get_listing(self, _):
            raise RuntimeError("UNEXPECTED_EOF_WHILE_READING")
    from analyzer.pipeline import process_job
    from analyzer.queue import _now
    for i in range(3):
        store.jobs[jid]["run_after"] = _now()
        job = store.claim_next_job("t")
        process_job(job, store, Boom(), amazon, llm)
        if i < 2:
            assert store.jobs[jid]["status"] == "queued"
    assert store.jobs[jid]["status"] == "failed" and store.jobs[jid]["attempts"] == 3


class _FakeEtsy:
    """模拟分页接口：listing 共 n 条，shop 共 300 条。"""
    def __init__(self, n):
        from analyzer.providers.mock import MockEtsy
        self.m, self.n, self.calls = MockEtsy(), n, []

    def make(self):
        from analyzer.providers.base import EtsyProvider
        outer = self

        class E(EtsyProvider):
            def get_listing(self, lid, slug=None):
                return outer.m.get_listing(lid, slug)

            def fetch_listing_reviews(self, lid, limit, offset):
                outer.calls.append(("listing", offset))
                return [outer.m._gen(lid, i) for i in range(offset, min(outer.n, offset + limit))], outer.n

            def fetch_shop_reviews(self, sid, limit, offset):
                outer.calls.append(("shop", offset))
                return [outer.m._gen("s", i) for i in range(offset, min(300, offset + limit))], 300
        return E()


def test_reviews_min_50_from_120():
    f = _FakeEtsy(120); e = f.make()
    rs = e.get_reviews(e.get_listing(1), min_reviews=50)
    assert len(rs) >= 50 and rs.listing_total == 120 and rs.shop_used == 0
    assert f.calls == [("listing", 0)]  # 一页 100 条已满足，不再翻页


def test_reviews_paginates_when_min_exceeds_page():
    f = _FakeEtsy(250); e = f.make()
    rs = e.get_reviews(e.get_listing(1), min_reviews=150)
    assert len(rs) == 200 and [c[1] for c in f.calls] == [0, 100]


def test_reviews_fewer_than_min_uses_all_and_optional_shop_supplement():
    f = _FakeEtsy(23); e = f.make(); p = e.get_listing(1)
    rs = e.get_reviews(p, min_reviews=50)
    assert len(rs) == 23 and rs.listing_used == 23 and rs.shop_used == 0
    rs2 = e.get_reviews(p, min_reviews=50, supplement_shop=True)
    assert rs2.listing_used == 23 and rs2.shop_used == 27 and len(rs2) == 50
    assert all(r.source == "listing" for r in rs2[:23])


def test_audience_summary_counts():
    from analyzer.audience import infer_audience, audience_summary
    f = _FakeEtsy(23); e = f.make(); p = e.get_listing(1)
    aud, ev = infer_audience(p, e.get_reviews(p, 50, supplement_shop=True))
    s = audience_summary(ev, aud)
    assert "分析了 50 条评论（商品共 23 条）" in s and "27 条为同店" in s
