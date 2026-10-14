"""EHunt 429 区分：每秒限速（重试/回队列，不熔断）vs 今日额度用完（熔断）；全局限速器；删除中的任务安静结束；预筛选优先领取。"""
import threading
import time
from unittest import mock

from analyzer.config import Config
from analyzer.http import HttpError
from analyzer.main import run_loop
from analyzer.pipeline import process_job
from analyzer.providers import build_providers, ehunt
from analyzer.providers.ehunt import EHuntEtsy, EHuntQuotaError, EHuntRateLimited, is_daily_exhausted
from analyzer.queue import MemoryStore, _now


def ok(lid, used=98, rem=102):
    return {"code": 200, "quota": {"used_today": used, "remaining_today": rem},
            "data": {"list": [{"title": "T", "price": "9", "reviews": 3, "product_url": f"https://www.etsy.com/listing/{lid}/x"}]}}


RATE_BODY = {"code": 429, "message": "Too Many Requests"}  # 线上实际：code 429、无 quota 字段


def test_classifier():
    assert not is_daily_exhausted("Too Many Requests")
    assert not is_daily_exhausted("code=429 message=None quota=None", used=98)
    assert not is_daily_exhausted("rate limit: 2 requests per second")
    assert is_daily_exhausted("daily quota exceeded")
    assert is_daily_exhausted("今日额度已用完")
    assert is_daily_exhausted("whatever", used=200)
    assert is_daily_exhausted("whatever", remaining=0)


def test_body_429_after_used_98_retries_then_succeeds():
    e = EHuntEtsy("k")
    e.used_today = 98
    with mock.patch.object(ehunt, "request_json", side_effect=[RATE_BODY, RATE_BODY, ok(42)]) as rq:
        p = e.get_listing(42)
    assert rq.call_count == 3 and p.title == "T" and e.remaining == 102


def test_persistent_rate_limit_raises_rate_limited_not_breaker():
    with mock.patch.object(ehunt, "request_json", return_value=RATE_BODY):
        try:
            EHuntEtsy("k").get_listing(42)
            assert False
        except EHuntRateLimited as ex:
            assert not getattr(ex, "breaker", False)


def test_http_429_with_used_200_is_breaker():
    e = EHuntEtsy("k")
    e.used_today = 200
    with mock.patch.object(ehunt, "request_json", side_effect=HttpError("HTTP 429 POST x: {}")) as rq:
        try:
            e.get_listing(42)
            assert False
        except EHuntQuotaError as ex:
            assert ex.breaker and rq.call_count == 1


def test_secret_not_in_error():
    with mock.patch.object(ehunt, "request_json", side_effect=HttpError("HTTP 429 POST x: SECRETKEY too many requests")):
        try:
            EHuntEtsy("SECRETKEY").get_listing(42)
        except EHuntRateLimited as ex:
            assert "SECRETKEY" not in str(ex)


def test_rate_limited_job_requeued_without_breaker_or_attempt():
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/42/a")
    _, amazon, llm = build_providers(Config())
    with mock.patch.object(ehunt, "request_json", return_value=RATE_BODY):
        run_loop(store, (EHuntEtsy("k"), amazon, llm), Config(), once=True)
    j = store.jobs[jid]
    assert j["status"] == "queued" and j["attempts"] == 0 and j["run_after"] > _now()
    assert not store.status.get("ehunt_paused_until")


def test_global_limiter_serializes_threads(monkeypatch):
    monkeypatch.setattr(ehunt, "MIN_INTERVAL", 0.05)
    monkeypatch.setattr(ehunt, "_sleep", time.sleep)
    active, peak, stamps = [0], [0], []
    lk = threading.Lock()
    def api(*a, **kw):
        with lk:
            active[0] += 1; peak[0] = max(peak[0], active[0]); stamps.append(time.monotonic())
        time.sleep(0.01)
        with lk:
            active[0] -= 1
        return ok(kw["body"]["search_key"].split("/listing/")[1])
    with mock.patch.object(ehunt, "request_json", side_effect=api):
        ts = [threading.Thread(target=EHuntEtsy("k").get_listing, args=(i,)) for i in range(1, 5)]
        [t.start() for t in ts]; [t.join() for t in ts]
    stamps.sort()
    assert peak[0] == 1 and all(b - a >= 0.045 for a, b in zip(stamps, stamps[1:]))


def test_deleted_job_fk_409_stops_quietly():
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/42/a")
    etsy, amazon, llm = build_providers(Config())
    job = store.claim_next_job("w")
    def boom(job, rows):
        store.jobs.pop(jid)
        raise HttpError('HTTP 409 POST x/evidence_snapshots: {"code":"23503","details":"Key (job_id) is not present in table \\"jobs\\"."}')
    store.save_snapshots = boom
    calls = []
    store.update_job = lambda *a, **k: calls.append((a, k))
    assert process_job(job, store, etsy, amazon, llm) is False
    assert not any(k.get("status") in ("failed", "queued") for _, k in calls)


def test_prescreen_claimed_before_full_analysis():
    store = MemoryStore()
    full = store.enqueue("https://www.etsy.com/listing/1/a")
    pre = store.enqueue("https://www.etsy.com/listing/2/b")
    kept = store.enqueue("https://www.etsy.com/listing/3/c")
    store.jobs[pre]["prescreen"] = True
    store.jobs[kept].update(prescreen=True, screen_state="kept")
    assert store.claim_next_job("w")["id"] == pre
    assert store.claim_next_job("w")["id"] == full
