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
    assert store.jobs[jid]["status"] == "failed" and store.jobs[jid]["attempts"] == 3
