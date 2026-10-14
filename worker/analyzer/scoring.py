"""竞争/需求评估（规则引擎，可替换/叠加 LLM 判断）。
注意：亚马逊不公开销量，est_monthly_sales 均为 BSR 推算的估值。"""
from .models import Assessment

IDENTICAL_T, SIMILAR_T = 0.55, 0.2


def estimate_monthly_sales_from_bsr(bsr, category=""):
    """粗略 BSR→月销量 幂律估算（未按类目校准），仅作排序参考。"""
    if not bsr or bsr <= 0:
        return None
    return max(1, int(150000 * (bsr ** -0.85)))


def classify(items):
    """按相似度把候选分为同款/类似品，低于阈值的丢弃。"""
    out = []
    for it in items:
        s = it.similarity or 0
        if s >= IDENTICAL_T:
            it.match_type = "identical"
        elif s >= SIMILAR_T:
            it.match_type = "similar"
        else:
            continue
        out.append(it)
    return out


def assess(items) -> Assessment:
    if not items:
        return Assessment("low", "low", 50, "亚马逊未找到相似产品：可能是蓝海，也可能无需求，需人工验证",
                          ["没有相似度达到阈值的亚马逊商品"])
    reviews = sorted(i.review_count or 0 for i in items)
    med_reviews = reviews[len(reviews) // 2]
    sales = [i.est_monthly_sales or 0 for i in items]
    top_sales = sum(sorted(sales, reverse=True)[:5])
    n_ident = sum(1 for i in items if i.match_type == "identical")
    comp = "high" if med_reviews > 1000 or n_ident >= 5 else "medium" if med_reviews > 200 or n_ident >= 2 else "low"
    demand = "high" if top_sales > 3000 else "medium" if top_sales > 500 else "low"
    score = {"low": 40, "medium": 20, "high": 0}[comp] + {"low": 10, "medium": 35, "high": 60}[demand]
    verdict = {
        (True, True): "需求不错且竞争可控，建议深入调研",
        (True, False): "有需求但竞争激烈，需差异化",
        (False, True): "竞争低但需求弱，谨慎",
        (False, False): "需求弱且竞争激烈，不建议",
    }[(demand != "low", comp != "high")]
    reasoning = [
        f"共 {len(items)} 个匹配（同款 {n_ident}，类似品 {len(items) - n_ident}）",
        f"评论数中位数 {med_reviews} → 竞争 {comp}（>1000 或同款≥5 为 high；>200 或同款≥2 为 medium）",
        f"销量估算前 5 合计 ≈{top_sales}/月（BSR 推算）→ 需求 {demand}（>3000 high；>500 medium）",
        f"机会分 = 竞争分 {({'low': 40, 'medium': 20, 'high': 0})[comp]} + 需求分 {({'low': 10, 'medium': 35, 'high': 60})[demand]} = {min(100, score)}",
    ]
    return Assessment(comp, demand, min(100, score), verdict, reasoning)
