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


CURRENT = {"job": None}


def heartbeat_loop(store, cfg, etsy, stop_evt):
    """后台线程：每 heartbeat_interval 秒写 worker_heartbeat，前端据此显示“分析服务离线”。"""
    while not stop_evt.is_set():
        try:
            store.heartbeat(cfg.worker_id, CURRENT["job"], {"ehunt_remaining": getattr(etsy, "remaining", None),
                                                            "pid": __import__("os").getpid()})
        except Exception:  # noqa: BLE001
            log.warning("heartbeat failed", exc_info=True)
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


def run_loop(store, providers, cfg, once=False):
    etsy, amazon, llm = providers[:3]
    shot = providers[3] if len(providers) > 3 else None
    while not _stop:
        job = None
        try:  # 熔断判断在 claim_next_job 内：熔断中只领取 24h 缓存内的 listing（0 credit），过期自动恢复
            job = store.claim_next_job(cfg.worker_id)
        except Exception:  # noqa: BLE001 网络抖动
            log.exception("claim failed")
        if job:
            log.info("processing %s %s", job["id"], job["etsy_url"])
            CURRENT["job"] = job["id"]
            try:
                process_job(job, store, etsy, amazon, llm, cfg.max_amazon_results, shot, cfg.max_snapshots_per_kind,
                            cfg.min_reviews, cfg.supplement_shop_reviews, cfg.ehunt_min_remaining)
            finally:
                CURRENT["job"] = None
            continue  # 有活就连续干
        if once:
            return
        time.sleep(cfg.poll_interval)


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
    providers = (*build_providers(cfg, cache=StoreCache(store)), build_screenshot(cfg))
    stop_evt = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(store, cfg, providers[0], stop_evt), daemon=True).start()
    log.info("worker %s polling every %ss", cfg.worker_id, cfg.poll_interval)
    try:
        run_loop(store, providers, cfg)
    finally:
        stop_evt.set()


if __name__ == "__main__":
    main()
