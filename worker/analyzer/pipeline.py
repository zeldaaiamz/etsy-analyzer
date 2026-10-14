import logging
from dataclasses import asdict
from .etsy_url import parse_listing_id, parse_slug
from .scoring import assess, classify
from .queue import _now, retry_delay
from .audience import audience_summary
from .providers.mock import MockScreenshot

log = logging.getLogger("pipeline")
LEVEL = {"low": "低", "medium": "中", "high": "高"}


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


def process_job(job, store, etsy, amazon, llm, max_results=10, shot=None, snapshot_limit=0):
    jid = job["id"]
    shot = shot or MockScreenshot()
    trace = []
    try:
        lid = parse_listing_id(job["etsy_url"])
        if not lid:
            raise PermanentError("无法从 URL 解析 Etsy listing id")
        store.update_job(jid, stage="fetch_etsy", etsy_listing_id=lid)
        prod = _get_listing(etsy, lid, parse_slug(job["etsy_url"]))
        reviews = etsy.get_reviews(prod)
        store.save_etsy_product(job, {**{k: v for k, v in asdict(prod).items()},
                                      "reviews": [asdict(r) for r in reviews]})

        store.update_job(jid, stage="analyze")
        an = llm.analyze_product(prod, reviews)
        ev = an.audience_evidence
        trace.append({"step": "identify", "title": "识别产品",
                      "input": {"listing_id": lid, "标题": prod.title, "标签": prod.tags, "价格": prod.price},
                      "output": {"产品": an.product_summary, "类目": an.category},
                      "reasoning": f"依据 Etsy 标题、标签、描述（{type(llm).__name__}）"})
        trace.append({"step": "audience", "title": "判断人群",
                      "input": {"评论数": ev.get("reviews_analyzed", 0), "来源": ev.get("source")},
                      "output": {"人群": an.target_audience, "主要信号": [s["signal"] for s in ev.get("signals", [])[:5]]},
                      "reasoning": ev.get("method", "")})
        trace.append({"step": "keywords", "title": "生成搜索词",
                      "input": {"标签": prod.tags, "人群": an.target_audience},
                      "output": {"搜索词": an.keywords[:3]},
                      "reasoning": an.keyword_reasoning or "取最能代表产品的英文词组"})

        store.update_job(jid, stage="search_amazon")
        pages, items, seen = [], [], set()
        for kw in an.keywords[:3]:
            pg = amazon.search(kw, limit=max_results)
            pages.append(pg)
            for it in pg.items:
                if it.asin and it.asin not in seen:
                    seen.add(it.asin)
                    items.append(it)
        trace.append({"step": "search", "title": "亚马逊搜索",
                      "input": {"搜索词": [p.keyword for p in pages], "数据源": getattr(amazon, "name", "")},
                      "output": {p.keyword: f"约 {p.total_results} 个结果，取前 {len(p.items)}" for p in pages},
                      "reasoning": f"去重后共 {len(items)} 个候选 ASIN"})

        ranked = llm.rank_similarity(prod, items)
        matches = classify(ranked)[:max_results]
        ni = sum(1 for m in matches if m.match_type == "identical")
        trace.append({"step": "match", "title": "匹配同款/类似",
                      "input": {"候选数": len(items), "阈值": "相似度≥0.55 同款；≥0.2 类似品"},
                      "output": {"同款": ni, "类似品": len(matches) - ni, "丢弃": len(items) - len(matches)},
                      "reasoning": "; ".join(f"{m.asin}:{m.similarity}({m.similarity_reason})" for m in matches[:5])})

        store.update_job(jid, stage="score")
        a = assess(matches)
        trace.append({"step": "assess", "title": "竞争与销量评估",
                      "input": {"匹配数": len(matches)},
                      "output": {"竞争": LEVEL[a.competition_level], "需求": LEVEL[a.demand_level], "机会分": a.opportunity_score},
                      "reasoning": a.reasoning})
        trace.append({"step": "conclusion", "title": "结论",
                      "input": {"竞争": LEVEL[a.competition_level], "需求": LEVEL[a.demand_level]},
                      "output": {"结论": a.verdict, "机会分": a.opportunity_score},
                      "reasoning": f"{audience_summary(ev, an.target_audience)}；{a.reasoning[1]}；{a.reasoning[2]}。"
                                   "注意：销量为 BSR 估算值。"})

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
        permanent = isinstance(e, PermanentError) or job.get("attempts", 1) >= job.get("max_attempts", 3)
        if permanent:
            store.update_job(jid, status="failed", error=str(e)[:1000], finished_at=_now(), locked_by=None)
        else:
            store.update_job(jid, status="queued", error=str(e)[:1000], locked_by=None,
                             run_after=_now() + retry_delay(job.get("attempts", 1)))
        return False
