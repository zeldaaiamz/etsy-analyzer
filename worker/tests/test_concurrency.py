"""M4：worker 并发——多线程领取不重复、Keepamore 全局限速、证据图并发且顺序不变。"""
import threading
import time
from collections import Counter
from analyzer.config import Config
from analyzer.providers import build_providers
from analyzer.providers.mock import MockScreenshot
from analyzer.queue import MemoryStore
from analyzer import main as M
from analyzer import pipeline as P


class SlowShot(MockScreenshot):
    """每张图 30ms，记录最大并发数。"""
    def __init__(self):
        self.cur = self.peak = 0
        self.lock = threading.Lock()

    def capture(self, *a, **k):
        with self.lock:
            self.cur += 1
            self.peak = max(self.peak, self.cur)
        time.sleep(0.03)
        with self.lock:
            self.cur -= 1
        return super().capture(*a, **k)


class CountingStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.claims = Counter()
        self.by = {}

    def claim_next_job(self, worker_id):
        j = super().claim_next_job(worker_id)
        if j:
            self.claims[j["id"]] += 1
            self.by[j["id"]] = worker_id
        return j


def _make(cfg, shot=None):
    return lambda: (*build_providers(cfg), shot or MockScreenshot())


def test_worker_ids():
    cfg = Config(worker_id="w")
    assert M.worker_ids(cfg, 1) == ["w"]
    assert M.worker_ids(cfg, 3) == ["w", "w-2", "w-3"]


def test_parallel_workers_each_job_once():
    cfg = Config(worker_id="t")
    store = CountingStore()
    ids = [store.enqueue(f"https://www.etsy.com/listing/{1000 + i}/x") for i in range(12)]
    M.run_workers(store, _make(cfg, SlowShot()), cfg, n=3, once=True)
    assert all(store.jobs[i]["status"] == "succeeded" for i in ids)
    assert all(store.claims[i] == 1 for i in ids), "同一任务被领取多次"
    assert len(set(store.by.values())) > 1, "应由多个线程分担"


def test_parallel_faster_than_serial():
    cfg = Config(worker_id="s")
    def run(n):
        st = MemoryStore()
        for i in range(6):
            st.enqueue(f"https://www.etsy.com/listing/{2000 + i}/x")
        t = time.monotonic()
        M.run_workers(st, _make(cfg, SlowShot()), cfg, n=n, once=True)
        return time.monotonic() - t
    serial, par = run(1), run(3)
    assert par < serial * 0.75, (serial, par)


def test_snapshots_parallel_same_order_and_paths():
    cfg = Config()
    shot = SlowShot()
    st1, st2 = MemoryStore(), MemoryStore()
    j1 = st1.enqueue("https://www.etsy.com/listing/3000/x")
    st2.jobs[j1] = dict(st1.jobs[j1])
    M.run_loop(st1, (*build_providers(cfg), MockScreenshot()), cfg, once=True)
    old = P.SNAPSHOT_THREADS
    try:
        P.SNAPSHOT_THREADS = 1
        M.run_loop(st2, (*build_providers(cfg), MockScreenshot()), cfg, once=True)
    finally:
        P.SNAPSHOT_THREADS = old
    key = lambda rows: [(r["kind"], r["image_path"], r.get("asin")) for r in rows]  # noqa: E731
    assert key(st1.snapshots) == key(st2.snapshots) and st1.snapshots
    # 直接调用：并发度 > 1
    job = {"id": "j", "user_id": "u"}
    pages = [type("Pg", (), {"keyword": f"k{i}", "url": "u", "total_results": 1, "items": []})() for i in range(4)]
    rows = P._snapshots(job, MemoryStore(), shot, pages, [], 0, threads=4)
    assert [r["image_path"] for r in rows] == [f"u/j/{n:03d}_search_result.svg" for n in range(1, 5)]
    assert shot.peak > 1


def test_keepamore_throttle_global_across_threads(monkeypatch):
    import analyzer.providers.keepamore as km
    monkeypatch.setattr(km, "MIN_INTERVAL", 0.05)
    km._rl_last[0] = 0.0
    stamps, lock = [], threading.Lock()
    def go():
        for _ in range(3):
            km._throttle()
            with lock:
                stamps.append(time.monotonic())
    ths = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 12 and min(gaps) >= 0.04, gaps  # 12 次请求在 4 个线程间仍保持 ≥ 间隔（20 次/分钟语义）
    assert stamps[-1] - stamps[0] >= 0.05 * 11 * 0.9


def test_throttle_does_not_hold_lock_while_waiting(monkeypatch):
    import analyzer.providers.keepamore as km
    monkeypatch.setattr(km, "MIN_INTERVAL", 0.3)
    km._rl_last[0] = time.monotonic()  # 下一个槽位在 0.3s 后
    t = threading.Thread(target=km._throttle)
    t.start()
    time.sleep(0.02)
    got = km._rl_lock.acquire(timeout=0.05)  # 等待中的线程不占锁
    assert got
    km._rl_lock.release()
    t.join()


def test_purge_expired_cache_memory():
    from datetime import timedelta
    from analyzer.queue import _now
    st = MemoryStore()
    st.cache_put("a", "x", {}, 60)
    st.kv["b"] = (_now() - timedelta(seconds=1), {})
    assert st.purge_expired_cache() == 1 and "a" in st.kv and "b" not in st.kv


def test_heartbeat_per_thread_and_purge():
    cfg = Config(worker_id="h", heartbeat_interval=0.01, cache_purge_hours=0.0)
    st = MemoryStore()
    ev = threading.Event()
    th = threading.Thread(target=M.heartbeat_loop, args=(st, cfg, None, ev, ["h", "h-2"]))
    th.start(); time.sleep(0.05); ev.set(); th.join()
    assert set(st.heartbeats) == {"h", "h-2"}
