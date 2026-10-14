"""目标人群推断（规则版，mock 与 LLM 失败时的兜底）。输出结构化依据。"""
import re

SIGNALS = [
    (r"\b(daughter|son|kids?|grandkids?|grandson|granddaughter)\b", "为孩子/孙辈购买", "父母、祖父母"),
    (r"\b(wife|girlfriend|her|mom|mother|sister)\b", "送给女性亲友", "为女性送礼的人"),
    (r"\b(husband|boyfriend|dad|father)\b", "送给男性亲友", "为男性送礼的人"),
    (r"\b(teacher|classroom|students?|coworkers?|office)\b", "老师/同事场景", "老师、职场人群"),
    (r"\b(halloween|christmas|easter|valentine'?s?|birthday|wedding|baby shower)\b", "节日/活动场景", "节日送礼与派对筹备者"),
    (r"\b(gift|gifted|present)\b", "作为礼物购买", "送礼人群"),
    (r"\b(decor|wall|home|living room|nursery)\b", "家居装饰用途", "家居装饰爱好者"),
    (r"\b(personali[sz]ed|custom|name)\b", "看重个性化定制", "偏好定制化的买家"),
]


def infer_audience(product, reviews):
    texts = [(r.text, r.rating) for r in reviews if r.text]
    src_of = {r.text: getattr(r, "source", "listing") for r in reviews}
    source = "reviews" if texts else "listing_text"
    corpus = texts or [(f"{product.title}. {' '.join(product.tags)}. {product.description[:500]}", None)]
    hits = {}
    evidence = []
    for text, rating in corpus:
        for pat, signal, aud in SIGNALS:
            m = re.search(pat, text, re.I)
            if m:
                hits.setdefault(signal, [aud, 0])[1] += 1
                if len(evidence) < 10 and source == "reviews" and not any(e["signal"] == signal for e in evidence[-3:]):
                    s = max(0, m.start() - 60)
                    evidence.append({"quote": ("…" if s else "") + text[s:m.end() + 60].strip() + "…",
                                     "rating": rating, "signal": signal, "source": src_of.get(text, "listing")})
    ranked = sorted(hits.items(), key=lambda kv: -kv[1][1])
    audiences = list(dict.fromkeys(v[0] for _, v in ranked))[:3] or ["通用消费者（信号不足）"]
    signals = [{"signal": k, "count": v[1], "audience": v[0]} for k, v in ranked]
    if source == "listing_text":
        evidence = [{"quote": f"标题/标签：{product.title} | {', '.join(product.tags[:8])}", "rating": None,
                     "signal": "; ".join(s["signal"] for s in signals) or "无"}]
    st = reviews.stats() if hasattr(reviews, "stats") else {"listing_reviews_total": len(texts), "listing_reviews_used": len(texts), "shop_reviews_used": 0}
    ev = {
        "source": source,
        "reviews_analyzed": len(texts),
        **st,
        "listing_fields_used": [] if texts else ["title", "tags", "description"],
        "method": ("规则匹配：在 %d 条评论中检索购买动机/收礼人/场景关键词并计数（规则：至少 %s 条，不足则全部）"
                   % (len(texts), st.get("min_reviews", 50))) if texts
                  else "无可用评论，改为从商品标题、标签、描述中检索人群关键词（可信度较低）",
        "signals": signals,
        "evidence": evidence,
    }
    return "、".join(audiences), ev


def review_count_text(ev):
    n, m, k = ev.get("reviews_analyzed", 0), ev.get("listing_reviews_total"), ev.get("shop_reviews_used", 0)
    t = f"{ev['no_text_provider']} 无评论原文，" if ev.get("no_text_provider") else ""
    t += f"分析了 {n} 条评论（商品共 {m if m is not None else '?'} 条）"
    if k:
        t += f"，其中 {k} 条为同店其它商品评论补充"
    return t


def audience_summary(ev, audience):
    if ev.get("source") == "reviews":
        return f"{review_count_text(ev)} → 人群：{audience}"
    if ev.get("no_text_provider"):
        return f"{review_count_text(ev)}，依据商品文字（{'/'.join(ev.get('listing_fields_used', []))}）→ 人群：{audience}"
    return f"无评论（商品共 0 条），依据商品文字（{'/'.join(ev.get('listing_fields_used', []))}）→ 人群：{audience}"
