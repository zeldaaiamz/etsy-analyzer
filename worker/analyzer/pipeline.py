import logging
from dataclasses import asdict
from .etsy_url import parse_listing_id, parse_slug
from .scoring import assess, classify
from .queue import _now, retry_delay
from .audience import audience_summary, review_count_text
from .providers.mock import MockScreenshot

log = logging.getLogger("pipeline")
LEVEL = {"low": "低", "medium": "中", "high": "高"}


def sales_source_note(matches):
    """按匹配逐个说明销量来源：monthlySold（亚马逊“过去一月购买”）还是 BSR 估算。"""
    ms = [m.asin for m in matches if (m.raw or {}).get("sales_source") == "monthlySold"]
    est = [m.asin for m in matches if m.asin not in ms and m.est_monthly_sales]
    none = len(matches) - len(ms) - len(est)
    if not matches:
        return "无匹配商品，无销量数据。"
    parts = []
    if ms:
        parts.append(f"{len(ms)} 个用亚马逊“过去一月购买”(monthlySold)：{'、'.join(ms[:5])}{'…' if len(ms) > 5 else ''}")
    if est:
        parts.append(f"{len(est)} 个为 BSR 估算值：{'、'.join(est[:5])}{'…' if len(est) > 5 else ''}")
    if none:
        parts.append(f"{none} 个无销量数据")
    return f"销量来源（{len(matches)} 个匹配）：" + "；".join(parts) + "。"


class PermanentError(Exception):
    """不可重试的错误（如 URL 非法）。"""


def _get_listing(etsy, lid, slug):
    try:
        return etsy.get_listing(lid, slug=slug)
    except TypeError:
        return etsy.get_listing(lid)


def _snapshots(job, store, shot, pages, matches, limit):
    rows, n = [], 0
    def add(kind, url, title, image_url, meta, extra):
        nonlocal n
        n += 1
        data, ct, src = shot.capture(kind, url, title, image_url, meta)
        ext = {"image/svg+xml": "svg", "image/png": "png", "image/webp": "webp"}.get(ct, "jpg")
        path = f"{job['user_id']}/{job['id']}/{n:03d}_{kind}.{ext}"
        store.upload_image(path, data, ct)
        rows.append({"kind": kind, "source_url": url, "title": title, "image_path": path, "image_source": src, **extra})
    for pg in pages:
        top = pg.items[:3]
        add("search_result", pg.url, f"亚马逊搜索：{pg.keyword}", "",
            {"关键词": pg.keyword, "结果总数": pg.total_results,
             **{f"Top{i + 1}": f"{it.title[:40]} ${it.price} BSR {it.bsr}" for i, it in enumerate(top)}},
            {"keyword": pg.keyword, "similarity_reason": f"关键词“{pg.keyword}”的搜索结果页，约 {pg.total_results or '?'} 个结果",
             "provider": pg.items[0].provider if pg.items else None})
    counts = {}
    for it in matches:
        counts[it.match_type] = counts.get(it.match_type, 0) + 1
        if limit and counts[it.match_type] > limit:
            continue
        add(it.match_type, it.url, it.title, it.image_url,
            {"ASIN": it.asin, "价格": f"${it.price}", "BSR": it.bsr, "评论": it.review_count, "相似度": it.similarity},
            {"keyword": it.keyword, "asin": it.asin, "price": it.price, "bsr": it.bsr, "review_count": it.review_count,
             "rating": it.rating, "similarity": it.similarity, "similarity_reason": it.similarity_reason,
             "provider": it.provider})
    store.save_snapshots(job, rows)
    return rows


def process_job(job, store, etsy, amazon, llm, max_results=10, shot=None, snapshot_limit=0,
                min_reviews=50, supplement_shop=False):
    jid = job["id"]
    shot = shot or MockScreenshot()
    trace = []
    try:
        lid = parse_listing_id(job["etsy_url"])
        if not lid:
            raise PermanentError("无法从 URL 解析 Etsy listing id")
        store.update_job(jid, status="running", stage="fetch_etsy", etsy_listing_id=lid)  # 领取时已是 running，这里再确认一次
        prod = _get_listing(etsy, lid, parse_slug(job["etsy_url"]))
        reviews = etsy.get_reviews(prod, min_reviews=min_reviews, supplement_shop=supplement_shop)
        store.save_etsy_product(job, {**{k: v for k, v in asdict(prod).items()},
                                      "reviews": [asdict(r) for r in reviews]})

        store.update_job(jid, stage="analyze")
        an = llm.analyze_product(prod, reviews)
        ev = an.audience_evidence
        if getattr(etsy, "no_review_text", False):
            ev["no_text_provider"] = "EHunt"
            ev["method"] = f"{review_count_text(ev)}；" + ev.get("method", "")
        em = (prod.raw or {}).get("ehunt") or {}
        etsy_metrics = {k: v for k, v in {"周销量": em.get("sales_weekly"), "总销量": em.get("sales_total"),
                        "收藏": em.get("favorites"), "周收藏": em.get("favorites_weekly"),
                        "评论数": em.get("reviews"), "周评论": em.get("reviews_weekly"),
                        "畅销": em.get("is_bestsell"), "Etsy 精选": em.get("is_pick"),
                        "好评如潮": em.get("is_raving"), "上架": em.get("release_time")}.items() if v not in (None, "")}
        if getattr(etsy, "remaining", None) is not None:
            etsy_metrics["EHunt 今日剩余额度"] = etsy.remaining
        trace.append({"step": "identify", "title": "识别产品",
                      "input": {"listing_id": lid, "标题": prod.title, "标签": prod.tags, "价格": prod.price, **etsy_metrics},
                      "output": {"产品": an.product_summary, "类目": an.category},
                      "reasoning": f"依据 Etsy 标题、标签、描述（{type(llm).__name__}）"})
        trace.append({"step": "audience", "title": "判断人群",
                      "input": {"评论": review_count_text(ev) if ev.get("source") == "reviews" or ev.get("no_text_provider") else "无评论，改用商品文字",
                                "本商品评论": ev.get("listing_reviews_used", 0), "同店补充评论": ev.get("shop_reviews_used", 0),
                                "规则": f"至少 {min_reviews} 条，不足则全部" + ("；允许同店补充" if supplement_shop else "；不使用同店补充")},
                      "output": {"人群": an.target_audience, "主要信号": [s["signal"] for s in ev.get("signals", [])[:5]]},
                      "reasoning": ev.get("method", "")})
        trace.append({"step": "keywords", "title": "生成搜索词",
                      "input": {"标签": prod.tags, "人群": an.target_audience},
                      "output": {"搜索词": an.keywords[:3]},
                      "reasoning": an.keyword_reasoning or "取最能代表产品的英文词组"})

        store.update_job(jid, stage="search_amazon")
        if hasattr(amazon, "reset_usage"):
            amazon.reset_usage()
        pages, items, seen = [], [], set()
        for kw in an.keywords[:3]:
            pg = amazon.search(kw, limit=max_results)
            pages.append(pg)
            for it in pg.items:
                if it.asin and it.asin not in seen:
                    seen.add(it.asin)
                    items.append(it)
        trace.append({"step": "search", "title": "亚马逊搜索",
                      "input": {"搜索词": [p.keyword for p in pages], "数据源": getattr(amazon, "name", ""),
                                **({"API 用量": {"调用": amazon.calls, "units": amazon.units_used, "剩余": amazon.remaining}}
                                   if hasattr(amazon, "units_used") else {})},
                      "output": {p.keyword: f"约 {p.total_results} 个结果，取前 {len(p.items)}" for p in pages},
                      "reasoning": f"去重后共 {len(items)} 个候选 ASIN" + (
                          f"；{amazon.usage_text()}" if hasattr(amazon, "usage_text") else "") + (
                          f"；销量信号：{sum(1 for i in items if (i.raw or {}).get('sales_source') == 'monthlySold')} 个用亚马逊“过去一月购买”(monthlySold)，其余用 BSR 估算"
                          if hasattr(amazon, "usage_text") else "")})

        ranked = llm.rank_similarity(prod, items)
        matches = classify(ranked)[:max_results]
        ni = sum(1 for m in matches if m.match_type == "identical")
        trace.append({"step": "match", "title": "匹配同款/类似",
                      "input": {"候选数": len(items), "方法": "LLM 批量判定 同款/类似品/无关" if type(llm).__name__ == "OpenAICompatLLM" else "标题词重叠：≥0.55 同款；≥0.2 类似品"},
                      "output": {"同款": ni, "类似品": len(matches) - ni, "丢弃": len(items) - len(matches)},
                      "reasoning": "; ".join(f"{m.asin}:{m.similarity}({m.similarity_reason})" for m in matches[:5])})

        store.update_job(jid, stage="score")
        a = assess(matches, em)
        trace.append({"step": "assess", "title": "竞争与销量评估",
                      "input": {"匹配数": len(matches)},
                      "output": {"竞争": LEVEL[a.competition_level], "需求": LEVEL[a.demand_level], "机会分": a.opportunity_score},
                      "reasoning": a.reasoning})
        trace.append({"step": "conclusion", "title": "结论",
                      "input": {"竞争": LEVEL[a.competition_level], "需求": LEVEL[a.demand_level]},
                      "output": {"结论": a.verdict, "机会分": a.opportunity_score},
                      "reasoning": f"{audience_summary(ev, an.target_audience)}；{a.reasoning[1]}；{a.reasoning[2]}" + (f"；{a.reasoning[-1]}" if a.reasoning[-1].startswith("证据薄弱") else "") + "。" + sales_source_note(matches)})

        store.update_job(jid, stage="snapshots")
        _snapshots(job, store, shot, pages, matches, snapshot_limit)
        store.save_amazon_matches(job, [{k: v for k, v in asdict(i).items()} for i in matches])
        an_d = asdict(an)
        an_d.pop("keyword_reasoning", None)
        a_d = asdict(a)
        a_d.pop("reasoning", None)
        store.save_analysis(job, {**an_d, **a_d, "llm_model": type(llm).__name__, "decision_trace": trace})
        store.update_job(jid, status="succeeded", stage="done", finished_at=_now(), locked_by=None)
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("job %s failed", jid)
        permanent = isinstance(e, PermanentError) or type(e).__name__ == "EHuntQuotaError" or job.get("attempts", 1) >= job.get("max_attempts", 3)
        if permanent:
            store.update_job(jid, status="failed", error=str(e)[:1000], finished_at=_now(), locked_by=None)
        else:
            store.update_job(jid, status="queued", error=str(e)[:1000], locked_by=None,
                             run_after=_now() + retry_delay(job.get("attempts", 1)))
        return False
