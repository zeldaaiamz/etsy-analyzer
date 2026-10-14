"""额度耗尽 → 暂停领取（任务保持排队、不消耗 attempts）→ 熔断到期自动恢复；缓存命中的任务在熔断中照常处理；
过期后首探仍 429 → 只再熔断 1 小时（自适应实际重置时间）。时钟用 mock 控制。"""
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from analyzer import queue, pipeline
from analyzer.cache import StoreCache
from analyzer.config import Config
from analyzer.http import HttpError
from analyzer.main import run_loop
from analyzer.providers import build_providers, ehunt
from analyzer.providers.ehunt import EHuntEtsy
from analyzer.queue import MemoryStore

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
MID = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)


def resp(lid, used, rem):
    return {"code": 200, "quota": {"used_today": used, "remaining_today": rem},
            "data": {"list": [{"title": f"L{lid}", "price": "9", "reviews": 3, "product_url": f"https://www.etsy.com/listing/{lid}/x"}]}}


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


def run(store, etsy, clock, side_effect):
    _, amazon, llm = build_providers(Config())
    with mock.patch.object(queue, "_now", clock), mock.patch.object(pipeline, "_now", clock), \
         mock.patch.object(ehunt, "request_json", side_effect=side_effect) as rq:
        run_loop(store, (etsy, amazon, llm), Config(), once=True)
    return rq.call_count


def setup(n):
    store = MemoryStore()
    ids = [store.enqueue(f"https://www.etsy.com/listing/{100 + i}/x") for i in range(n)]
    for j in store.jobs.values():
        j["run_after"] = j["created_at"] = j["queued_at"] = T0 - timedelta(minutes=10) + timedelta(seconds=len(ids))
    for i, jid in enumerate(ids):
        store.jobs[jid]["created_at"] = T0 - timedelta(minutes=10) + timedelta(seconds=i)
    return store, ids, EHuntEtsy("k", cache=StoreCache(store))


def lid_of(kw):
    return int(kw["body"]["search_key"].split("/listing/")[1].split("/")[0])


def test_exhaust_then_auto_resume_next_day():
    store, ids, etsy = setup(5)
    clock = Clock(T0)
    used = {"n": 197}
    def api(*a, **kw):  # 剩 3 个额度
        used["n"] += 1
        return resp(lid_of(kw), used["n"], 200 - used["n"])
    assert run(store, etsy, clock, api) == 3
    st = [store.jobs[i]["status"] for i in ids]
    assert st == ["succeeded"] * 3 + ["queued"] * 2
    assert all(store.jobs[i]["attempts"] == 0 for i in ids[3:])          # 未消耗 attempts
    assert store.status["ehunt_paused_until"] == MID and store.status["ehunt_used_today"] == 200
    clock.t = MID - timedelta(minutes=1)
    assert run(store, etsy, clock, api) == 0                               # 午夜前不领取
    clock.t = MID + timedelta(seconds=5); used["n"] = 0                    # 额度刷新
    assert run(store, etsy, clock, api) == 2
    assert all(store.jobs[i]["status"] == "succeeded" for i in ids)
    assert store.status["ehunt_paused_until"] is None and store.status["ehunt_remaining"] == 198


def test_cached_listing_processed_while_exhausted():
    store, ids, etsy = setup(3)
    clock = Clock(T0)
    store.status["ehunt_paused_until"] = MID
    with mock.patch.object(queue, "_now", clock):
        etsy.cache.put("ehunt:listing:101", "ehunt", {"listing_id": 101, "title": "cached", "description": "", "price": 9,
                                                       "currency": "USD", "tags": [], "shop_name": "", "shop_id": None,
                                                       "image_url": "", "num_favorers": 1, "raw": {}}, 86400)
    assert run(store, etsy, clock, AssertionError("no EHunt call")) == 0
    assert [store.jobs[i]["status"] for i in ids] == ["queued", "succeeded", "queued"]
    assert store.status["ehunt_paused_until"] == MID                       # 缓存命中不改熔断


def test_probe_after_expiry_rearms_one_hour_then_resumes():
    store, ids, etsy = setup(2)
    clock = Clock(MID + timedelta(seconds=5))
    store.status["ehunt_paused_until"] = MID                               # 已过期
    assert run(store, etsy, clock, HttpError('HTTP 429 POST x: {"code":429,"message":"daily quota exceeded"}')) == 1
    assert store.status["ehunt_paused_until"] == clock.t + timedelta(hours=1)
    assert store.jobs[ids[0]]["status"] == "queued" and store.jobs[ids[0]]["attempts"] == 0
    clock.t += timedelta(minutes=30)
    assert run(store, etsy, clock, HttpError('HTTP 429 POST x: {"code":429,"message":"daily quota exceeded"}')) == 0
    clock.t += timedelta(minutes=31)                                       # 仍未重置 → 再 1 小时
    assert run(store, etsy, clock, HttpError('HTTP 429 POST x: {"code":429,"message":"daily quota exceeded"}')) == 1
    assert store.status["ehunt_paused_until"] == clock.t + timedelta(hours=1)
    clock.t += timedelta(hours=1, seconds=1)
    assert run(store, etsy, clock, lambda *a, **kw: resp(lid_of(kw), 1, 199)) == 2
    assert all(store.jobs[i]["status"] == "succeeded" for i in ids)


def test_fresh_429_breaks_until_utc_midnight():
    store, ids, etsy = setup(1)
    clock = Clock(T0)
    run(store, etsy, clock, HttpError('HTTP 429 POST x: {"code":429,"message":"daily quota exceeded"}'))
    assert store.status["ehunt_paused_until"] == MID and store.jobs[ids[0]]["run_after"] == MID
