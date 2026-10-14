"""Worker 入口：轮询 Supabase 队列（只出站，无需入站端口）。
python -m analyzer.main            # 常驻轮询
python -m analyzer.main --demo URL [URL...]   # mock 模式下用内存队列跑一遍并打印结果
"""
import argparse
import json
import logging
import signal
import time
from .config import Config
from .providers import build_providers, build_screenshot
from .queue import MemoryStore, SupabaseStore
from .pipeline import process_job

log = logging.getLogger("worker")
_stop = False


def _sig(*_):
    global _stop
    _stop = True


def run_loop(store, providers, cfg, once=False):
    etsy, amazon, llm = providers[:3]
    shot = providers[3] if len(providers) > 3 else None
    while not _stop:
        try:
            job = store.claim_next_job(cfg.worker_id)
        except Exception:  # noqa: BLE001 网络抖动
            log.exception("claim failed")
            job = None
        if job:
            log.info("processing %s %s", job["id"], job["etsy_url"])
            process_job(job, store, etsy, amazon, llm, cfg.max_amazon_results, shot, cfg.max_snapshots_per_kind)
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
    providers = (*build_providers(cfg), build_screenshot(cfg))
    if a.demo is not None or cfg.mode == "mock":
        store = MemoryStore()
        for u in a.demo or ["https://www.etsy.com/listing/1234567890/line-art-print"]:
            store.enqueue(u)
        run_loop(store, providers, cfg, once=True)
        print(json.dumps({"jobs": list(store.jobs.values()), "analyses": store.analyses,
                          "matches": store.matches}, ensure_ascii=False, indent=1, default=str))
        return store
    store = SupabaseStore(cfg.supabase_url, cfg.supabase_key, cfg.storage_bucket)
    log.info("worker %s polling every %ss", cfg.worker_id, cfg.poll_interval)
    run_loop(store, providers, cfg)


if __name__ == "__main__":
    main()
