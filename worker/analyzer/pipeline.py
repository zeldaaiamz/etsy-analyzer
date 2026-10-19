import logging
import os
from dataclasses import asdict, fields as dc_fields
from .models import EtsyProduct
from .etsy_url import parse_listing_id, parse_slug, is_short, resolve_short
from .errors import PermanentError, JobCanceled, is_retryable
from .scoring import assess, classify
from datetime import timedelta
from .queue import _now, retry_delay, next_utc_midnight
from .audience import audience_summary, review_count_text
from .providers.mock import MockScreenshot
from .amazon_keywords import build as build_keywords

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


def _usage(p):
    return (getattr(p, "calls", 0) or 0, getattr(p, "units_used", None), getattr(p, "cache_hits", 0) or 0)


def _bump(store, name, before, after):
    calls, hits = after[0] - before[0], after[2] - before[2]
    units = (after[1] or 0) - (before[1] or 0) if after[1] is not None else calls
    if calls or hits:
        try:
            store.bump_usage(name, calls, units, hits)
        except Exception:  # noqa: BLE001
            log.warning("bump_usage failed", exc_info=True)


PROBE_RETRY = timedelta(hours=1)
RATE_LIMIT_RETRY = timedelta(seconds=60)
ANOMALY_RETRY = timedelta(minutes=float(os.environ.get("EHUNT_ANOMALY_RETRY_MINUTES", "30")))


def _job_gone(store, jid, e):
    """失败时判断任务是否已被删除：外键错误（23503 / is not present in table "jobs"）或库里查不到。"""
    s = str(e)
    if not ("23503" in s or "HTTP 409" in s or "HTTP 404" in s):
        return False
    try:
        return not store.job_exists(jid)
    except Exception:  # noqa: BLE001
        return False


def _check_ehunt_remaining(store, etsy, threshold, before_calls=None):
    """每次真实 EHunt 调用后写入最新额度（前端额度条）；剩余 ≤ 阈值则熔断到次日 UTC 0 点。
    真实调用成功且仍有余量 → 清除已过期的熔断（自动恢复）。缓存命中不改状态。"""
    rem = getattr(etsy, "remaining", None)
    if getattr(etsy, "name", "") != "ehunt":
        return
    if before_calls is not None and getattr(etsy, "calls", 0) == before_calls:
        return  # 本任务未发生真实调用（缓存命中/失败），状态不变
    if rem is None:
        if before_calls is None:
            return
        try:  # 真实调用成功（code 200）但响应没带 quota：EHunt 已恢复 → 解除熔断，额度记为未知（前端显示“未知”）
            store.set_service_status(ehunt_paused_until=None, reason=None, ehunt_remaining=None,
                                     ehunt_used_today=None, ehunt_quota_at=_now())
        except Exception:  # noqa: BLE001
            log.warning("set_service_status failed", exc_info=True)
        return
    try:
        f = {"ehunt_remaining": rem, "ehunt_used_today": getattr(etsy, "used_today", None), "ehunt_quota_at": _now()}
        if rem <= threshold:
            f.update(ehunt_paused_until=next_utc_midnight(), reason=f"EHunt 今日额度已用完（剩余 {rem}），额度刷新后自动继续")
        else:
            f.update(ehunt_paused_until=None, reason=None)
        store.set_service_status(**f)
    except Exception:  # noqa: BLE001
        log.warning("set_service_status failed", exc_info=True)


def breaker_until(store, now=None):
    """429 熔断到何时：默认下一个 UTC 0 点（EHunt 文档未写重置时间）。
    若上一次熔断已过期（= 这是过期后的首次探测）仍 429 → 说明实际重置更晚，只再等 1 小时后重探。"""
    now = now or _now()
    try:
        pu = (store.get_service_status() or {}).get("ehunt_paused_until")
    except Exception:  # noqa: BLE001
        pu = None
    if isinstance(pu, str):
        from datetime import datetime
        pu = datetime.fromisoformat(pu.replace("Z", "+00:00"))
    if pu and pu <= now:
        return now + PROBE_RETRY
    return next_utc_midnight(now)


def _get_listing(etsy, lid, slug):
    try:
        return etsy.get_listing(lid, slug=slug)
    except TypeError:
        return etsy.get_listing(lid)


SNAPSHOT_THREADS = 4


def _snapshots(job, store, shot, pages, matches, limit, threads=SNAPSHOT_THREADS):
    """证据图：每张图（下载/截图 + 上传）互不依赖 → 线程池并发；编号与顺序和串行时完全一致。"""
    specs = []
    def add(kind, url, title, image_url, meta, extra):
        specs.append((len(specs) + 1, kind, url, title, image_url, meta, extra))
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

    def one(spec):
        n, kind, url, title, image_url, meta, extra = spec
        data, ct, src = shot.capture(kind, url, title, image_url, meta)
        ext = {"image/svg+xml": "svg", "image/png": "png", "image/webp": "webp"}.get(ct, "jpg")
        path = f"{job['user_id']}/{job['id']}/{n:03d}_{kind}.{ext}"
        store.upload_image(path, data, ct)
        return {"kind": kind, "source_url": url, "title": title, "image_path": path, "image_source": src, **extra}

    if threads > 1 and len(specs) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(threads, len(specs)), thread_name_prefix="snap") as ex:
            rows = list(ex.map(one, specs))  # map 保持顺序；任何一张失败都会抛出（与串行行为一致）
    else:
        rows = [one(sp) for sp in specs]
    store.save_snapshots(job, rows)
    return rows


def fetch_stage(job, store, etsy, step, min_reviews=50, supplement_shop=False, ehunt_min_remaining=0):
    """取数段：解析 URL → EHunt（缓存优先）→ 评论 → 保存 etsy_products。"""
    url = job["etsy_url"]
    lid = parse_listing_id(url)
    if not lid and is_short(url):
        url = resolve_short(url) or ""
        lid = parse_listing_id(url)
        if not lid:
            raise PermanentError("etsy.me 短链无法展开，请在浏览器打开后复制完整商品地址")
    if not lid:
        raise PermanentError("无法从 URL 解析 Etsy listing id")
    step("fetch_etsy", status="running", etsy_listing_id=lid)
    c0 = getattr(etsy, "calls", 0)
    try:
        prod = _get_listing(etsy, lid, parse_slug(url))
    finally:
        _check_ehunt_remaining(store, etsy, ehunt_min_remaining, c0)
    reviews = etsy.get_reviews(prod, min_reviews=min_reviews, supplement_shop=supplement_shop)
    store.save_etsy_product(job, {**{k: v for k, v in asdict(prod).items()},
                                  "reviews": [asdict(r) for r in reviews]})
    return prod, reviews


def load_saved_product(store, etsy, job, min_reviews=50, supplement_shop=False):
    """分析段：从库里还原取数段保存的 EtsyProduct（0 次 EHunt 调用）。"""
    row = store.load_etsy_product(job)
    if not row or not row.get("listing_id"):
        return None, None
    fields = {f.name for f in dc_fields(EtsyProduct)}
    prod = EtsyProduct(**{k: v for k, v in row.items() if k in fields})
    prod.listing_id = int(prod.listing_id)
    prod.raw = dict(prod.raw or {})
    totals = getattr(etsy, "_totals", None)
    if isinstance(totals, dict):  # EHunt 无评论正文：评论总数来自已存的 ehunt 指标
        totals.setdefault(prod.listing_id, (prod.raw.get("ehunt") or {}).get("reviews"))
    reviews = etsy.get_reviews(prod, min_reviews=min_reviews, supplement_shop=supplement_shop)
    return prod, reviews


def process_job(job, store, etsy, amazon, llm, max_results=10, shot=None, snapshot_limit=0,
                min_reviews=50, supplement_shop=False, ehunt_min_remaining=0):
    jid = job["id"]
    shot = shot or MockScreenshot()
    trace = []
    u0 = {n: _usage(p) for n, p in (("ehunt", etsy), ("keepamore", amazon), ("deepseek", llm))}

    def step(stage, **extra):
        """阶段切换：先检查取消请求，再更新 stage + locked_at（心跳）。"""
        if store.is_cancel_requested(jid):
            raise JobCanceled()
        store.update_job(jid, stage=stage, locked_at=_now(), **extra)

    if hasattr(amazon, "on_wait"):
        amazon.on_wait = lambda: store.update_job(jid, locked_at=_now())
    try:
        pre, kept = bool(job.get("prescreen")), job.get("screen_state") == "kept"
        if pre and kept:
            # 分析段：EHunt 数据已在取数段入库 → 直接读库，不再调 EHunt（额度/熔断都不影响）
            step("analyze", status="running")
            prod, reviews = load_saved_product(store, etsy, job, min_reviews, supplement_shop)
            if prod is None:  # 理论上不会发生（取数段已保存）；兜底走缓存优先的取数
                prod, reviews = fetch_stage(job, store, etsy, step, min_reviews, supplement_shop, ehunt_min_remaining)
        else:
            prod, reviews = fetch_stage(job, store, etsy, step, min_reviews, supplement_shop, ehunt_min_remaining)
            if pre:
                # 预筛选：取数段到此为止，等用户在筛选页确认（screen_confirm）后再进入昂贵的分析段
                store.update_job(jid, status="screening", stage="awaiting_confirm", screen_state="awaiting",
                                 locked_by=None, error=None)
                return True
        lid = prod.listing_id
        url = job["etsy_url"]

        step("analyze")
        an = llm.analyze_product(prod, reviews)
        if not getattr(an, "keyword_items", None):  # 旧缓存/旧 provider：用纯字符串关键词兜底（来源 title + 原标题短语）
            an.keyword_items = build_keywords([], prod.title, fallback=an.keywords)
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
        if (prod.raw or {}).get("cache"):
            etsy_metrics["EHunt 缓存"] = "命中（24 小时内已查询，本次 0 credit）"
        if ev.get("llm_cache"):
            etsy_metrics["LLM 缓存"] = "命中（24 小时内已识别，本次 0 次调用）"
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
                      "output": {"搜索词": an.keywords[:3],
                                 "一键亚马逊搜索词": [f"D{k.get('direction', '?')} {k['keyword']}（{k['source']}）" for k in an.keyword_items]},
                      "reasoning": an.keyword_reasoning or "取最能代表产品的英文词组"})

        step("search_amazon")
        if hasattr(amazon, "reset_usage"):
            amazon.reset_usage()
            u0["keepamore"] = _usage(amazon)
        pages, items, seen = [], [], set()
        for i, kw in enumerate(an.keywords[:3]):
            if i:
                step("search_amazon")  # 每个关键词之间也检查取消 + 心跳
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

        step("match")
        ranked = llm.rank_similarity(prod, items)
        matches = classify(ranked)[:max_results]
        ni = sum(1 for m in matches if m.match_type == "identical")
        trace.append({"step": "match", "title": "匹配同款/类似",
                      "input": {"候选数": len(items), "方法": "LLM 批量判定 同款/类似品/无关" if type(llm).__name__ == "OpenAICompatLLM" else "标题词重叠：≥0.55 同款；≥0.2 类似品"},
                      "output": {"同款": ni, "类似品": len(matches) - ni, "丢弃": len(items) - len(matches)},
                      "reasoning": "; ".join(f"{m.asin}:{m.similarity}({m.similarity_reason})" for m in matches[:5])})

        step("score")
        a = assess(matches, em)
        trace.append({"step": "assess", "title": "竞争与销量评估",
                      "input": {"匹配数": len(matches)},
                      "output": {"竞争": LEVEL[a.competition_level], "需求": LEVEL[a.demand_level], "机会分": a.opportunity_score},
                      "reasoning": a.reasoning})
        trace.append({"step": "conclusion", "title": "结论",
                      "input": {"竞争": LEVEL[a.competition_level], "需求": LEVEL[a.demand_level]},
                      "output": {"结论": a.verdict, "机会分": a.opportunity_score},
                      "reasoning": f"{audience_summary(ev, an.target_audience)}；{a.reasoning[1]}；{a.reasoning[2]}" + (f"；{a.reasoning[-1]}" if a.reasoning[-1].startswith("证据薄弱") else "") + "。" + sales_source_note(matches)})

        step("snapshots")
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="matches") as ex:  # 匹配写库与证据图并行
            fm = ex.submit(store.save_amazon_matches, job, [{k: v for k, v in asdict(i).items()} for i in matches])
            _snapshots(job, store, shot, pages, matches, snapshot_limit)
            fm.result()
        an_d = asdict(an)
        an_d.pop("keyword_reasoning", None)
        a_d = asdict(a)
        a_d.pop("reasoning", None)
        store.save_analysis(job, {**an_d, **a_d, "llm_model": type(llm).__name__, "decision_trace": trace})
        store.update_job(jid, status="succeeded", stage="done", finished_at=_now(), locked_by=None)
        return True
    except JobCanceled:
        log.info("job %s canceled by user", jid)
        store.update_job(jid, status="canceled", finished_at=_now(), locked_by=None,
                         error="已按你的请求取消（取消前已发出的请求可能已计费）")
        return False
    except Exception as e:  # noqa: BLE001
        attempts = job.get("attempts", 1)
        if _job_gone(store, jid, e):  # 任务在运行中被用户删除（如 evidence_snapshots 外键 23503）→ 安静结束
            log.info("job %s no longer exists (deleted mid-run); stopping quietly", jid)
            return False
        if getattr(e, "rate_limited", False):  # EHunt 每秒限速等非额度 429：不熔断，回队列 1 分钟后再试，不计尝试次数
            log.warning("job %s: %s; requeued", jid, e)
            store.update_job(jid, status="queued", stage=None, error=str(e)[:500], locked_by=None,
                             attempts=max(0, attempts - 1), run_after=_now() + RATE_LIMIT_RETRY)
            return False
        if getattr(e, "service_anomaly", False):  # EHunt 账号/服务异常：全局熔断 ANOMALY_RETRY，不计尝试次数，不判永久失败
            until = _now() + ANOMALY_RETRY
            log.warning("job %s: %s; EHunt circuit open until %s (service anomaly)", jid, e, until)
            try:  # 只写熔断与原因；保留 used/remaining/quota_at 作为“上次正常”的额度（前端 tooltip 展示，不当作当前额度）
                store.set_service_status(ehunt_paused_until=until, reason=str(e)[:300])
            except Exception:  # noqa: BLE001
                log.warning("set_service_status failed", exc_info=True)
            store.update_job(jid, status="queued", stage=None, error=f"{str(e)[:400]}，已暂停取数，自动重试",
                             locked_by=None, attempts=max(0, attempts - 1), run_after=until)
            return False
        if getattr(e, "breaker", False):  # EHunt 429：全局熔断到次日（UTC），本任务回到队列且不计入尝试次数
            until = breaker_until(store)
            log.warning("EHunt quota exhausted; circuit open until %s", until)
            try:
                store.set_service_status(ehunt_paused_until=until, ehunt_remaining=0, ehunt_quota_at=_now(),
                                         reason="EHunt 今日额度已用完，额度刷新后自动继续")
            except Exception:  # noqa: BLE001
                log.warning("set_service_status failed", exc_info=True)
            store.update_job(jid, status="queued", stage=None, error="EHunt 今日额度已用完，明日自动继续",
                             locked_by=None, attempts=max(0, attempts - 1), run_after=until)
            return False
        retry = is_retryable(e, attempts) and attempts < job.get("max_attempts", 3)
        log.log(logging.ERROR if retry else logging.WARNING, "job %s failed (%s): %s", jid,
                "will retry" if retry else "permanent", e, exc_info=not isinstance(e, PermanentError))
        if not retry and is_retryable(e, attempts) and attempts >= job.get("max_attempts", 3):
            # 第 max_attempts 次仍失败：自动删除任务（产品库内的 listing 则标记 failed 保留）
            try:
                action = store.discard_exhausted_job(jid, str(e)[:1000])
                log.warning("job %s exhausted %s attempts -> %s", jid, attempts, action)
            except Exception:  # noqa: BLE001
                log.warning("discard_exhausted_job failed; marking failed", exc_info=True)
                store.update_job(jid, status="failed", error=str(e)[:1000], finished_at=_now(), locked_by=None)
        elif not retry:
            store.update_job(jid, status="failed", error=str(e)[:1000], finished_at=_now(), locked_by=None)
        else:
            store.update_job(jid, status="queued", error=str(e)[:1000], locked_by=None,
                             run_after=_now() + retry_delay(attempts))
        return False
    finally:
        for n, p in (("ehunt", etsy), ("keepamore", amazon), ("deepseek", llm)):
            if type(p).__module__.endswith(".mock"):
                continue
            _bump(store, n, u0[n], _usage(p))
