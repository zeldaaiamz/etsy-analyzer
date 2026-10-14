from analyzer.queue import MemoryStore
from analyzer.pipeline import sales_source_note
from analyzer.models import AmazonItem


def test_claim_sets_running_and_started_and_skips_paused():
    s = MemoryStore()
    a = s.enqueue("https://www.etsy.com/listing/1/a")
    b = s.enqueue("https://www.etsy.com/listing/2/b")
    s.jobs[a]["status"] = "paused"
    j = s.claim_next_job("w")
    assert j["id"] == b and j["status"] == "running" and j["started_at"] and j["finished_at"] is None
    assert s.claim_next_job("w") is None


def test_sales_source_note():
    m1 = AmazonItem(asin="A1", title="t", est_monthly_sales=300, raw={"sales_source": "monthlySold"})
    m2 = AmazonItem(asin="B2", title="t", est_monthly_sales=40, raw={"sales_source": "bsr_estimate"})
    m3 = AmazonItem(asin="C3", title="t")
    t = sales_source_note([m1, m2, m3])
    assert "1 个用亚马逊“过去一月购买”(monthlySold)：A1" in t and "1 个为 BSR 估算值：B2" in t and "1 个无销量数据" in t
