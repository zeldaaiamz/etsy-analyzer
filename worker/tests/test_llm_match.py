from analyzer.models import AmazonItem, EtsyProduct
from analyzer.providers.llm_openai import OpenAICompatLLM
from analyzer.scoring import assess, classify


def test_llm_batch_match_labels():
    l = OpenAICompatLLM("http://x", "k", "m")
    calls = []
    l._chat = lambda s, u: calls.append(u) or {"results": [{"i": 0, "label": "同款", "reason": "同一产品"},
                                                             {"i": 1, "label": "无关", "reason": "品类不同"}]}
    items = [AmazonItem("A", "Mug"), AmazonItem("B", "Sock"), AmazonItem("C", "Cup")]
    out = classify(l.rank_similarity(EtsyProduct(1, "Mug"), items))
    assert len(calls) == 1 and [i.asin for i in out] == ["A"] and out[0].match_type == "identical"


def test_weak_evidence_not_recommended():
    it = AmazonItem("A", "x", review_count=10, est_monthly_sales=2000, match_type="identical")
    a = assess([it, AmazonItem("B", "y", review_count=5, est_monthly_sales=1500, match_type="similar")],
               {"sales_total": 0, "reviews": 1})
    assert "建议深入调研" not in a.verdict and "证据" in a.verdict and a.opportunity_score <= 45
