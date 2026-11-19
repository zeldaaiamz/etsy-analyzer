"""2026-11-19 选品雷达接三步查同一张排除品类表：关键词命中 → 不查 EHunt、不调视觉；标题闸门默认关；任何失败放行。不联网。"""
import json, sys, urllib.error
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import category_gate as CG  # noqa: E402
from selection import ehunt_worker as W  # noqa: E402


def fake(action="drop", reason="带电设备", detail="heater", n=1, calls=None, raise_=None):
    def op(req, timeout):
        if calls is not None: calls.append((req.full_url, dict(req.header_items()), json.loads(req.data)))
        if raise_: raise raise_
        return json.dumps({"results": [{"action": action, "reason": reason, "detail": detail}] * n}).encode()
    return op


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("RF_CATEGORY_URL", "https://example.invalid/api/category-check")
    monkeypatch.setenv("RF_CATEGORY_TOKEN", "tok-0123456789abcdef0123")
    for k in ("SEL_CATEGORY_KEYWORD_MODE", "SEL_CATEGORY_TITLE_MODE"): monkeypatch.delenv(k, raising=False)
    CG._FAILS.update(n=0, until=0.0); CG._CACHE.clear()


def test_keyword_drop_blocks_and_sends_token():
    calls = []
    hit = CG.check_keyword("heater", opener=fake(calls=calls))
    assert hit["reason"] == "带电设备"
    url, hdr, body = calls[0]
    assert body == {"keywords": ["heater"]} and hdr.get("X-rf-service-token") == "tok-0123456789abcdef0123"
    assert "不做的品类（带电设备）" in CG.block_message(hit) and len(CG.block_message(hit)) <= 300


@pytest.mark.parametrize("action", ["keep", "doubt"])
def test_keep_and_doubt_pass(action):
    assert CG.check_keyword("book shelf", opener=fake(action=action)) is None


def test_log_and_off_modes(monkeypatch):
    monkeypatch.setenv("SEL_CATEGORY_KEYWORD_MODE", "log"); assert CG.check_keyword("heater", opener=fake()) is None
    calls = []
    monkeypatch.setenv("SEL_CATEGORY_KEYWORD_MODE", "off"); assert CG.check_keyword("heater", opener=fake(calls=calls)) is None
    assert calls == []


def test_not_configured_means_unchanged(monkeypatch):
    monkeypatch.delenv("RF_CATEGORY_URL"); calls = []
    assert CG.check_keyword("heater", opener=fake(calls=calls)) is None and calls == []


@pytest.mark.parametrize("err", [urllib.error.URLError("down"), TimeoutError("t"), ValueError("x")])
def test_errors_fail_open_and_breaker(err):
    calls = []
    for _ in range(3): assert CG.check_keyword("heater", opener=fake(calls=calls, raise_=err)) is None
    assert CG._FAILS["until"] > 0
    assert CG.check_keyword("heater", opener=fake(calls=calls)) is None and len(calls) == 3   # 熔断 10 分钟内不再调


def test_bad_response_fails_open():
    assert CG.check_keyword("heater", opener=fake(n=2)) is None
    assert CG.check_keyword("heater", opener=lambda r, t: b"<html>") is None


def test_title_default_off_log_exclude(monkeypatch):
    calls = []
    assert CG.title_rule("USB LED Strip Light", opener=fake(calls=calls)) is None and calls == []
    monkeypatch.setenv("SEL_CATEGORY_TITLE_MODE", "log"); assert CG.title_rule("USB LED Strip Light", opener=fake(calls=calls)) is None
    monkeypatch.setenv("SEL_CATEGORY_TITLE_MODE", "exclude")
    r = CG.title_rule("USB LED Strip Light", opener=fake(calls=calls))
    assert r[0] == "排除" and r[1] == "不做的品类" and len(calls) == 1   # 同一标题走缓存
    assert CG.title_rule("Planner Stickers", opener=fake(action="keep")) is None


class Store:
    def __init__(self): self.patches = []
    def patch_run_if(self, rid, expect, **kw): self.patches.append((rid, expect, kw))


class A: fake = None


def test_handle_run_blocked_keyword_never_opens_ehunt(monkeypatch):
    monkeypatch.setattr(CG, "check_keyword", lambda kw: {"reason": "带电设备", "detail": "heater", "mode": "block"})
    monkeypatch.setattr(W, "count_ehunt", lambda *a, **k: pytest.fail("不该查 EHunt"))
    s = Store(); W.handle_run(s, {"id": 7, "status": "counting", "keyword": "heater"}, A(), None)
    rid, expect, kw = s.patches[0]
    assert (rid, expect, kw["status"]) == (7, "counting", "failed") and "不做的品类" in kw["error"] and kw["progress"]["stage"] == "category_skipped"


def test_handle_run_pass_counts_as_before(monkeypatch):
    monkeypatch.setattr(CG, "check_keyword", lambda kw: None)
    monkeypatch.setattr(W, "count_ehunt", lambda kw, p: 123)
    s = Store(); W.handle_run(s, {"id": 8, "status": "counting", "keyword": "planner stickers"}, A(), None)
    assert s.patches[0][2]["status"] == "counted" and s.patches[0][2]["total_count"] == 123
