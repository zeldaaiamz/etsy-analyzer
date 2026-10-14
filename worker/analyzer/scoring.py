"""竞争/需求评估（规则引擎，可替换/叠加 LLM 判断）。
注意：亚马逊不公开销量，est_monthly_sales 均为 BSR 推算的估值。"""
from .models import Assessment


def estimate_monthly_sales_from_bsr(bsr, category=""):
    """粗略 BSR→月销量 幂律估算（Home & Kitchen 量级经验值），仅作排序参考。"""
    if not bsr or bsr <= 0:
        return None
    return max(1, int(150000 * (bsr ** -0.85)))


def assess(items) -> Assessment:
    if not items:
        return Assessment("low", "low", 50, "亚马逊未找到相似产品：可能是蓝海，也可能无需求，需人工验证")
    reviews = sorted(i.review_count or 0 for i in items)
    med_reviews = reviews[len(reviews) // 2]
    sales = [i.est_monthly_sales or 0 for i in items]
    top_sales = sum(sorted(sales, reverse=True)[:5])
    comp = "high" if med_reviews > 1000 else "medium" if med_reviews > 200 else "low"
    demand = "high" if top_sales > 3000 else "medium" if top_sales > 500 else "low"
    score = {"low": 40, "medium": 20, "high": 0}[comp] + {"low": 10, "medium": 35, "high": 60}[demand]
    verdict = {
        (True, True): "需求不错且竞争可控，建议深入调研",
        (True, False): "有需求但竞争激烈，需差异化",
        (False, True): "竞争低但需求弱，谨慎",
        (False, False): "需求弱且竞争激烈，不建议",
    }[(demand != "low", comp != "high")]
    return Assessment(comp, demand, min(100, score), verdict)
