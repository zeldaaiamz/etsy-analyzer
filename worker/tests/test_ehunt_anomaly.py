"""EHunt 账号/服务异常（2026-10-15 线上：HTTP 200 + body {"code":500,"message":"查询失败: 'daily_limit'"}，无 quota）：
熔断 + 回队列不耗尝试次数 + 不判永久失败；失败响应不计入 provider_usage_daily；恢复后自动解除熔断。"""
from unittest import mock

from analyzer.config import Config
from analyzer.errors import is_retryable
from analyzer.main import run_loop
from analyzer.pipeline import ANOMALY_RETRY
from analyzer.providers import build_providers, ehunt
from analyzer.providers.ehunt import ANOMALY_PREFIX, EHuntEtsy, EHuntServiceError, is_service_anomaly
from analyzer.queue import MemoryStore, _now

PROD_BODY = {"code": 500, "message": "查询失败: 'daily_limit'", "timestamp": 1, "data": None}


def ok(lid, used=5, rem=195):
    return {"code": 200, "quota": {"used_today": used, "remaining_today": rem},
            "data": {"list": [{"title": "T", "price": "9", "reviews": 3, "product_url": f"https://www.etsy.com/listing/{lid}/x"}]}}


def _run(store, body):
    _, amazon, llm = build_providers(Config())
    etsy = EHuntEtsy("SECRETKEY")
    with mock.patch.object(ehunt, "request_json", side_effect=body if isinstance(body, list) else None,
                           return_value=None if isinstance(body, list) else body) as rq:
        run_loop(store, (etsy, amazon, llm), Config(), once=True)
    return etsy, rq


def test_classifier():
    assert is_service_anomaly(500, "查询失败: 'daily_limit'")
    assert is_service_anomaly(502, "")
    assert is_service_anomaly(None, "查询失败")
    assert is_service_anomaly(400, "KeyError 'daily_limit'")
    assert is_service_anomaly(401, "无效的API Key")
    assert not is_service_anomaly(404, "not found")
    assert not is_service_anomaly(400, "参数错误")


def test_provider_raises_service_error_and_does_not_count_call():
    e = EHuntEtsy("SECRETKEY")
    e.used_today, e.remaining = 97, 103
    with mock.patch.object(ehunt, "request_json", return_value=PROD_BODY) as rq:
        try:
            e.get_listing(603690215)
            assert False
        except EHuntServiceError as ex:
            assert str(ex).startswith(ANOMALY_PREFIX) and "daily_limit" in str(ex) and "额度" not in str(ex)
            assert not is_retryable(ex)
    assert rq.call_count == 1  # 不在单条上重试
    assert e.calls == 0 and (e.used_today, e.remaining) == (97, 103)


def test_body_401_is_anomaly_and_secret_scrubbed():
    with mock.patch.object(ehunt, "request_json", return_value={"code": 401, "message": "SECRETKEY 无效的API Key"}):
        try:
            EHuntEtsy("SECRETKEY").get_listing(42)
            assert False
        except EHuntServiceError as ex:
            assert "SECRETKEY" not in str(ex) and "拒绝访问" in str(ex)


def test_job_requeued_breaker_tripped_attempts_not_consumed():
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/603690215/a")
    store.jobs[jid]["prescreen"] = True
    store.status.update(ehunt_remaining=103, ehunt_used_today=97)
    etsy, _ = _run(store, PROD_BODY)
    j = store.jobs[jid]
    assert j["status"] == "queued" and j["attempts"] == 0
    assert j["error"].startswith(ANOMALY_PREFIX) and "额度" not in j["error"]  # 不能被 claim/前端当作“等待额度”
    assert j["run_after"] > _now() + ANOMALY_RETRY - __import__("datetime").timedelta(seconds=5)
    st = store.status
    assert st["ehunt_paused_until"] == j["run_after"] and st["reason"].startswith(ANOMALY_PREFIX)
    assert (st["ehunt_remaining"], st["ehunt_used_today"]) == (103, 97)  # 保留上次正常额度（前端只在 tooltip 里显示）
    assert "ehunt_quota_at" not in st
    assert store.usage.get("ehunt", [0, 0, 0])[0] == 0  # 失败调用不计入 provider_usage_daily
    assert etsy.calls == 0


def test_repeated_anomaly_never_becomes_permanent():
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/42/a")
    for _ in range(5):
        store.jobs[jid]["run_after"] = _now()
        store.status["ehunt_paused_until"] = None
        _run(store, PROD_BODY)
    assert store.jobs[jid]["status"] == "queued" and store.jobs[jid]["attempts"] == 0


def test_paused_queue_not_claimed_until_breaker_expires():
    store = MemoryStore()
    a = store.enqueue("https://www.etsy.com/listing/1/a")
    b = store.enqueue("https://www.etsy.com/listing/2/b")
    _, rq = _run(store, PROD_BODY)
    assert rq.call_count == 1  # 第 1 条触发熔断后，第 2 条不再被领取（不浪费请求）
    assert store.claim_next_job("w") is None
    assert store.jobs[b]["status"] == "queued" and store.jobs[a]["status"] == "queued"


def test_recovery_clears_breaker_and_counts_only_success():
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/42/a")
    store.jobs[jid]["prescreen"] = True
    _run(store, PROD_BODY)
    store.jobs[jid]["run_after"] = _now()
    store.status["ehunt_paused_until"] = _now()  # 熔断到期 → 下一条任务即探测
    etsy, rq = _run(store, [ok(42)])
    assert rq.call_count == 1 and store.jobs[jid]["status"] == "screening"
    st = store.status
    assert st["ehunt_paused_until"] is None and st["reason"] is None
    assert (st["ehunt_used_today"], st["ehunt_remaining"]) == (5, 195)
    assert store.usage["ehunt"][0] == 1


def test_success_without_quota_clears_anomaly_and_marks_quota_unknown():
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/42/a")
    store.jobs[jid]["prescreen"] = True
    store.status.update(ehunt_paused_until=None, reason=f"{ANOMALY_PREFIX}（x）", ehunt_remaining=5)
    body = ok(42); body.pop("quota")
    _run(store, body)
    st = store.status
    assert st["reason"] is None and st["ehunt_remaining"] is None and st["ehunt_used_today"] is None


def test_other_business_errors_unchanged():
    store = MemoryStore()
    jid = store.enqueue("https://www.etsy.com/listing/42/a")
    _run(store, {"code": 400, "message": "参数错误"})
    j = store.jobs[jid]
    assert j["status"] == "failed" and "EHunt 错误 400" in j["error"]
    assert not store.status.get("ehunt_paused_until")
