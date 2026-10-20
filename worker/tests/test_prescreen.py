"""预筛选两段式（017）：取数段只调 EHunt 并停在 screening；确认后的分析段 0 次 EHunt；
排除的任务不再产生任何调用；熔断中已确认任务照常分析；普通任务仍走完整流程。"""
from datetime import timedelta
from unittest import mock

from analyzer import queue, pipeline
from analyzer.cache import StoreCache
from analyzer.config import Config
from analyzer.providers import build_providers, ehunt
from analyzer.providers.ehunt import EHuntEtsy
from analyzer.queue import MemoryStore
from tests.test_quota_resume import Clock, T0, MID, resp, lid_of


class Counting:
    """包一层 LLM/Amazon，统计昂贵阶段的调用次数。"""
    def __init__(self, inner): self.inner, self.n = inner, 0
    def __getattr__(self, k):
        v = getattr(self.inner, k)
        if callable(v) and k in ("analyze_product", "search", "rank_similarity"):
            def w(*a, **kw):
                self.n += 1
                return v(*a, **kw)
            return w
        return v


def run(store, etsy, clock, api):
    from analyzer.main import run_loop
    _, amazon, llm = build_providers(Config())
    amazon, llm = Counting(amazon), Counting(llm)
    with mock.patch.object(queue, "_now", clock), mock.patch.object(pipeline, "_now", clock), \
         mock.patch.object(ehunt, "request_json", side_effect=api) as rq:
        run_loop(store, (etsy, amazon, llm), Config(), once=True)
    return rq.call_count, amazon.n + llm.n


def confirm(store, clock, *a):
    """screen_confirm 用 queue._now 设置 run_after，必须与 run() 使用同一模拟时钟（否则真实时间晚于 T0 后任务永远不到期）。"""
    with mock.patch.object(queue, "_now", clock):
        return store.screen_confirm(*a)


def setup(n, prescreen=True):
    store = MemoryStore()
    ids = [store.enqueue(f"https://www.etsy.com/listing/{100 + i}/x", prescreen=prescreen) for i in range(n)]
    for i, jid in enumerate(ids):
        j = store.jobs[jid]
        j["run_after"] = j["queued_at"] = T0 - timedelta(minutes=10)
        j["created_at"] = T0 - timedelta(minutes=10) + timedelta(seconds=i)
    return store, ids, EHuntEtsy("k", cache=StoreCache(store))


def ok(*a, **kw):
    return resp(lid_of(kw), 1, 199)


def test_fetch_stage_stops_at_screening():
    store, ids, etsy = setup(3)
    calls, expensive = run(store, etsy, Clock(T0), ok)
    assert calls == 3 and expensive == 0
    for i in ids:
        j = store.jobs[i]
        assert (j["status"], j["stage"], j["screen_state"]) == ("screening", "awaiting_confirm", "awaiting")
        assert store.etsy[i]["title"].startswith("L1")
    assert not store.analyses and not store.snapshots


def test_confirm_analyzes_kept_without_refetch_and_rejected_stop():
    store, ids, etsy = setup(4)
    clock = Clock(T0)
    run(store, etsy, clock, ok)
    store.kv.clear()                                   # 缓存过期也不得重新取数
    assert confirm(store, clock, ids[:2], ids[2:3]) == {"kept": 2, "rejected": 1}
    calls, expensive = run(store, etsy, clock, AssertionError("no EHunt call in analyze stage"))
    assert calls == 0 and expensive > 0
    assert [store.jobs[i]["status"] for i in ids] == ["succeeded", "succeeded", "canceled", "screening"]
    assert store.jobs[ids[2]]["screen_state"] == "rejected"
    assert set(store.analyses) == set(ids[:2])
    assert {s["job_id"] for s in store.snapshots} <= set(ids[:2])
    # 已确认的再次确认无效；排除的不会被领取
    assert confirm(store, clock, ids[:3]) == {"kept": 0, "rejected": 0}
    assert run(store, etsy, clock, AssertionError("x")) == (0, 0)


def test_quota_exhausted_fetch_waits_but_kept_jobs_still_analyze():
    store, ids, etsy = setup(4)
    clock = Clock(T0)
    used = {"n": 197}
    def api(*a, **kw):
        used["n"] += 1
        return resp(lid_of(kw), used["n"], 200 - used["n"])
    calls, _ = run(store, etsy, clock, api)
    assert calls == 3
    assert [store.jobs[i]["status"] for i in ids] == ["screening"] * 3 + ["queued"]
    assert store.status["ehunt_paused_until"] == MID
    confirm(store, clock, ids[:3])
    calls, expensive = run(store, etsy, clock, AssertionError("paused"))   # 熔断中：已确认的照常分析
    assert calls == 0 and expensive > 0
    assert [store.jobs[i]["status"] for i in ids] == ["succeeded"] * 3 + ["queued"]
    clock.t = MID + timedelta(seconds=5); used["n"] = 0                    # 次日继续取数
    assert run(store, etsy, clock, api)[0] == 1
    assert store.jobs[ids[3]]["status"] == "screening"


def test_cached_listing_is_not_refetched_in_fetch_stage():
    store, ids, etsy = setup(2)
    clock = Clock(T0)
    run(store, etsy, clock, ok)
    s2, ids2, _ = setup(2)
    s2.kv = store.kv                                                        # 同 listing 再提交：24h 缓存命中
    etsy2 = EHuntEtsy("k", cache=StoreCache(s2))
    calls, _ = run(s2, etsy2, clock, AssertionError("cached"))
    assert calls == 0 and all(s2.jobs[i]["status"] == "screening" for i in ids2)


def test_normal_jobs_still_full_pipeline():
    store, ids, etsy = setup(2, prescreen=False)
    calls, expensive = run(store, etsy, Clock(T0), ok)
    assert calls == 2 and expensive > 0
    assert all(store.jobs[i]["status"] == "succeeded" for i in ids)


def test_supabase_claim_falls_back_when_v2_missing():
    from analyzer.http import HttpError
    from analyzer.queue import SupabaseStore
    s = SupabaseStore("https://x.supabase.co", "k")
    seen = []
    def call(method, path, body=None, params=None, prefer=""):
        seen.append(path)
        if path.endswith("_v2"):
            raise HttpError("HTTP 404 POST /rpc/claim_next_job_v2: PGRST202")
        return [{"id": "j1"}]
    s._call = call
    assert s.claim_next_job("w")["id"] == "j1" and s.v2 is False
    assert s.claim_next_job("w")["id"] == "j1"
    assert seen == ["/rpc/claim_next_job_v2", "/rpc/claim_next_job", "/rpc/claim_next_job"]


def test_reclaimed_job_reuses_saved_fetch_no_refetch():
    """042 除了获取失败，不重复获取：取数已成功保存后任务被重新认领（attempts>1，如 worker 中断/超时回收），不再调 EHunt。"""
    store, ids, etsy = setup(1)
    clock = Clock(T0)
    assert run(store, etsy, clock, ok)[0] == 1
    j = store.jobs[ids[0]]
    j.update(status="queued", stage=None, locked_by=None)            # 模拟重新排队/回收（attempts 已 = 1，下次认领 = 2）
    etsy2 = EHuntEtsy("k")                                          # 无缓存：只能靠已保存的 etsy_products
    calls, _ = run(store, etsy2, clock, AssertionError("refetch"))
    assert calls == 0 and store.jobs[ids[0]]["status"] == "screening"


def test_first_claim_does_not_read_saved_product():
    """首次认领不多查一次库（不拖慢）。"""
    store, ids, etsy = setup(1)
    with mock.patch.object(store, "load_etsy_product", side_effect=AssertionError("extra read")):
        assert run(store, etsy, Clock(T0), ok)[0] == 1
