"""Worker 入口：轮询 Supabase 队列（只出站，无需入站端口）。
python -m analyzer.main            # 常驻轮询
python -m analyzer.main --demo URL [URL...]   # mock 模式下用内存队列跑一遍并打印结果
"""
import argparse
import json
import logging
import signal
import threading
import time
from .config import Config
from .providers import build_providers, build_screenshot
from .queue import MemoryStore, SupabaseStore
from .pipeline import process_job
from .cache import StoreCache

log = logging.getLogger("worker")
_stop = False


def _sig(*_):
    global _stop
    _stop = True


CURRENT = {}  # worker_id -> 正在处理的 job id（多线程时每个线程一项）


def heartbeat_loop(store, cfg, etsy, stop_evt, worker_ids=None):
    """后台线程：每 heartbeat_interval 秒为每个 worker 线程写 worker_heartbeat，前端据此显示“分析服务离线”；
    顺带每 cache_purge_hours 小时清理一次 provider_cache 过期行（L5）。"""
    ids = worker_ids or [cfg.worker_id]
    next_purge = time.monotonic() + 60
    while not stop_evt.is_set():
        for wid in ids:
            try:
                store.heartbeat(wid, CURRENT.get(wid), {"ehunt_remaining": getattr(etsy, "remaining", None),
                                                        "pid": __import__("os").getpid(), "threads": len(ids)})
            except Exception:  # noqa: BLE001
                log.warning("heartbeat failed", exc_info=True)
        if cfg.cache_purge_hours and time.monotonic() >= next_purge:
            next_purge = time.monotonic() + cfg.cache_purge_hours * 3600
            try:
                n = store.purge_expired_cache()
                if n:
                    log.info("purged %s expired provider_cache rows", n)
            except Exception:  # noqa: BLE001
                log.warning("cache purge failed (migration 015 not applied?)", exc_info=True)
        stop_evt.wait(cfg.heartbeat_interval)


def ehunt_paused(store):
    """熔断/低额度检查（DB 的 claim_next_job 也会拦，这里避免空转日志）。"""
    try:
        st = store.get_service_status() or {}
    except Exception:  # noqa: BLE001
        return False
    pu = st.get("ehunt_paused_until")
    if not pu:
        return False
    if isinstance(pu, str):
        from datetime import datetime
        pu = datetime.fromisoformat(pu.replace("Z", "+00:00"))
    from .queue import _now
    return pu > _now()


def run_loop(store, providers, cfg, once=False, worker_id=None, stop_evt=None):
    """单个 worker 循环：领取 → 处理 → 继续领取；没活时 sleep poll_interval。
    多线程并发安全：claim_next_job 在 DB 里用 FOR UPDATE SKIP LOCKED（010），同一任务只会被一个线程领到。"""
    etsy, amazon, llm = providers[:3]
    shot = providers[3] if len(providers) > 3 else None
    wid = worker_id or cfg.worker_id
    while not _stop and not (stop_evt and stop_evt.is_set()):
        job = None
        try:  # 熔断判断在 claim_next_job 内：熔断中只领取 24h 缓存内的 listing（0 credit），过期自动恢复
            job = store.claim_next_job(wid)
        except Exception:  # noqa: BLE001 网络抖动
            log.exception("claim failed")
        if job:
            log.info("[%s] processing %s %s", wid, job["id"], job["etsy_url"])
            CURRENT[wid] = job["id"]
            try:
                process_job(job, store, etsy, amazon, llm, cfg.max_amazon_results, shot, cfg.max_snapshots_per_kind,
                            cfg.min_reviews, cfg.supplement_shop_reviews, cfg.ehunt_min_remaining)
            except Exception:  # noqa: BLE001  process_job 自己处理失败；这里兜底防止线程退出
                log.exception("[%s] unexpected error", wid)
            finally:
                CURRENT[wid] = None
            continue  # 有活就连续干
        if once:
            return
        if stop_evt:
            stop_evt.wait(cfg.poll_interval)
        else:
            time.sleep(cfg.poll_interval)


def worker_ids(cfg, n=None):
    n = max(1, n or getattr(cfg, "concurrency", 1))
    return [cfg.worker_id] + [f"{cfg.worker_id}-{i}" for i in range(2, n + 1)]


def run_workers(store, make_providers, cfg, n=None, once=False, stop_evt=None):
    """M4：n 个 worker 线程并发处理队列。每个线程有独立的 provider 实例（用量计数按任务独立），
    共享 store / provider_cache；Keepamore 的 20 次/分钟限速是进程级全局令牌（keepamore._throttle）。"""
    ids = worker_ids(cfg, n)
    if len(ids) == 1:
        run_loop(store, make_providers(), cfg, once=once, worker_id=ids[0], stop_evt=stop_evt)
        return ids
    ths = [threading.Thread(target=run_loop, name=wid, args=(store, make_providers(), cfg),
                            kwargs={"once": once, "worker_id": wid, "stop_evt": stop_evt}, daemon=True) for wid in ids]
    for t in ths:
        t.start()
    for t in ths:
        while t.is_alive():
            t.join(1.0)
            if _stop and stop_evt:
                stop_evt.set()
    return ids


def review_ocr_loop(cfg, stop_evt, interval=20.0):
    """后台线程：处理 review_ocr_jobs（019）队列。REVIEW_OCR_ENABLED=0 关闭；缺 pytesseract 时自动跳过。"""
    from . import review_ocr
    while not stop_evt.is_set():
        try:
            while not stop_evt.is_set() and review_ocr.run_once(cfg):
                pass
        except Exception:
            log.warning("review OCR loop error", exc_info=True)
        stop_evt.wait(interval)


def review_ocr_enabled():
    import importlib.util
    import os
    if os.environ.get("REVIEW_OCR_ENABLED", "1").strip() in ("0", "false", "no", ""):
        return False
    return importlib.util.find_spec("pytesseract") is not None


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", nargs="*", help="mock 演示：内存队列处理这些 URL 后退出")
    a = ap.parse_args(argv)
    cfg = Config.from_env()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    if a.demo is not None or cfg.mode == "mock":
        store = MemoryStore()
        providers = (*build_providers(cfg), build_screenshot(cfg))
        for u in a.demo or ["https://www.etsy.com/listing/1234567890/line-art-print"]:
            store.enqueue(u)
        run_loop(store, providers, cfg, once=True)
        print(json.dumps({"jobs": list(store.jobs.values()), "analyses": store.analyses,
                          "matches": store.matches}, ensure_ascii=False, indent=1, default=str))
        return store
    store = SupabaseStore(cfg.supabase_url, cfg.supabase_key, cfg.storage_bucket)
    cache = StoreCache(store)
    make = lambda: (*build_providers(cfg, cache=cache, store=store), build_screenshot(cfg))  # noqa: E731
    ids = worker_ids(cfg)
    first = make()
    stop_evt = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(store, cfg, first[0], stop_evt, ids), daemon=True).start()
    if review_ocr_enabled():
        threading.Thread(target=review_ocr_loop, args=(cfg, stop_evt), daemon=True).start()
        log.info("review OCR loop enabled")
    log.info("worker %s polling every %ss with %d thread(s)", cfg.worker_id, cfg.poll_interval, len(ids))
    pool = iter([first])
    try:
        run_workers(store, lambda: next(pool, None) or make(), cfg, stop_evt=stop_evt)
    finally:
        stop_evt.set()


if __name__ == "__main__":
    main()
