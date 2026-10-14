import logging
from dataclasses import asdict
from .etsy_url import parse_listing_id
from .scoring import assess
from .queue import _now, retry_delay

log = logging.getLogger("pipeline")


class PermanentError(Exception):
    """不可重试的错误（如 URL 非法）。"""


def process_job(job, store, etsy, amazon, llm, max_results=10):
    jid = job["id"]
    try:
        lid = parse_listing_id(job["etsy_url"])
        if not lid:
            raise PermanentError("无法从 URL 解析 Etsy listing id")
        store.update_job(jid, stage="fetch_etsy", etsy_listing_id=lid)
        prod = etsy.get_listing(lid)
        pd = asdict(prod)
        store.save_etsy_product(job, pd)

        store.update_job(jid, stage="analyze")
        an = llm.analyze_product(prod)

        store.update_job(jid, stage="search_amazon")
        items, seen = [], set()
        for kw in an.keywords[:3]:
            for it in amazon.search(kw, limit=max_results):
                if it.asin and it.asin not in seen:
                    seen.add(it.asin)
                    items.append(it)
        items = llm.rank_similarity(prod, items)[:max_results]

        store.update_job(jid, stage="score")
        a = assess(items)
        store.save_amazon_matches(job, [asdict(i) for i in items])
        store.save_analysis(job, {**asdict(an), **asdict(a), "llm_model": type(llm).__name__})
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
