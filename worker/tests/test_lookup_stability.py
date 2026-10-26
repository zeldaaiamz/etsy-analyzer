"""2026-10-22 预筛选取数稳定性：
① “Playwright Sync API inside the asyncio loop”：查询页打不开后重开时重复进入 sync_playwright（真实 Playwright 驱动复现，不启动浏览器）；
② 打不开查询页 → 已认领的放回队列而不是标 error；被污染 → 放回队列并抛 PlaywrightPoisoned；
③ 搜索页搜不到 → 商品详情页兜底；④ 自带浏览器缺失 → 系统 Chrome；⑤ 豆包长尾期间穿插查询；⑥ box 心跳 + ECS 端区分离线/忙。"""
import sys, threading, time
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402
from analyzer.providers.ehunt import EHuntServiceError  # noqa: E402
from analyzer.providers.ehunt_web import EHuntWebPending  # noqa: E402
from analyzer.queue import _now  # noqa: E402
from tests.test_ehunt_web import LookupStore, prov, IMG  # noqa: E402
from tests.test_sel_progress import RunStore  # noqa: E402

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


class Box:
    def __init__(self, ids): self.q, self.patches, self.log = [{"listing_id": i} for i in ids], {}, []
    def lookup_claim(self, n): out, self.q = self.q[:n], self.q[n:]; return out
    def lookup_patch(self, lid, **kw): self.patches[lid] = kw; self.log.append((lid, kw.get("status")))


class FakeCtx:
    def __init__(self): self.closed = False
    def close(self): self.closed = True


class FakePage:
    def __init__(self): self.closed = False
    def close(self): self.closed = True
    def is_closed(self): return self.closed


def _fake_finder(results):
    def finder(lid):
        r = results.get(lid, IMG)
        if isinstance(r, Exception): raise r
        return r
    finder.cleanup = lambda: None
    return finder


def test_reopen_after_page_error_does_not_reenter_sync_playwright(monkeypatch):
    """旧版：第 1 条出错 → 下一条重开 → 第二次 enter sync_playwright → asyncio loop 报错，余下全部 error。"""
    launches = []
    monkeypatch.setattr(W, "launch_ctx", lambda p, **k: launches.append(1) or FakeCtx())
    monkeypatch.setattr(W, "open_lookup_page", lambda c: FakePage())
    monkeypatch.setattr(W, "page_finder", lambda pg: _fake_finder({1: RuntimeError("page broke")}))
    st = Box([1, 2, 3])
    W.serve_lookups_browser(st)
    assert st.patches[1]["status"] == "error" and st.patches[2]["status"] == st.patches[3]["status"] == "done"
    assert all(W.ASYNCIO_LOOP_MSG not in (p.get("error") or "") for p in st.patches.values())
    assert launches == [1]                                    # 同一调用只开一次上下文
    assert not W.playwright_leaked()


def test_open_page_timeout_requeues_instead_of_error(monkeypatch):
    """2026-10-22 07:44：查询页 goto 60s 超时 → 旧版本轮 10 条全部 error（且触发 asyncio 报错）。现在放回队列，下一轮再试。"""
    monkeypatch.setattr(W, "launch_ctx", lambda p, **k: FakeCtx())
    def boom(c): raise TimeoutError("Page.goto: Timeout 60000ms exceeded.")
    monkeypatch.setattr(W, "open_lookup_page", boom)
    st = Box([1, 2, 3])
    assert W.serve_lookups_browser(st) == 0
    assert {k: v["status"] for k, v in st.patches.items()} == {1: "queued", 2: "queued", 3: "queued"}
    assert st.patches[1]["claimed_by"] is None and not W.playwright_leaked()
    # 下一轮：页面恢复 → 正常完成，且同一进程里再次 sync_playwright 不报错
    monkeypatch.setattr(W, "open_lookup_page", lambda c: FakePage())
    monkeypatch.setattr(W, "page_finder", lambda pg: _fake_finder({}))
    st.q = [{"listing_id": i} for i in (1, 2, 3)]
    assert W.serve_lookups_browser(st) == 3 and all(v["status"] == "done" for v in st.patches.values())


def test_poisoned_requeues_and_raises():
    st = Box([1, 2])
    def factory(): raise RuntimeError("It looks like you are using " + W.ASYNCIO_LOOP_MSG + ".")
    with pytest.raises(W.PlaywrightPoisoned): W.serve_lookups(st, factory)
    assert [s for _, s in st.log] == ["queued", "queued"]
    st2 = Box([1, 2])
    with pytest.raises(W.PlaywrightPoisoned):
        W.serve_lookups(st2, lambda: _fake_finder({1: RuntimeError(W.ASYNCIO_LOOP_MSG)}))
    assert st2.patches[1]["status"] == st2.patches[2]["status"] == "queued"


def test_leaked_loop_detected_before_lookup(monkeypatch):
    monkeypatch.setattr(W, "playwright_leaked", lambda: True)
    st = Box([5])
    with pytest.raises(W.PlaywrightPoisoned): W.serve_lookups_browser(st)
    assert st.patches[5]["status"] == "queued"


def test_pick_detail_image():
    srcs = ["https://ehunt.ai/logo.png", "https://i.etsystatic.com/6359666/r/il/929d7a/5484340591/il_fullxfull.5484340591_h6sh.jpg"]
    body = "Safari Shadow Play … Item Info: ID: 630614482"
    assert W.pick_detail_image(srcs, body, 630614482) == "https://i.etsystatic.com/6359666/r/il/929d7a/5484340591/il_570xN.5484340591_h6sh.jpg"
    assert W.pick_detail_image(srcs, body, 999) is None                 # 页面不是这条 listing
    assert W.pick_detail_image([], "Product is not included or invalid 4999999999", 4999999999) is None


def test_finder_falls_back_to_product_detail(monkeypatch):
    class Ctx:
        def __init__(self): self.pages = []
        def new_page(self): p = FakePage(); self.pages.append(p); return p
    class Pg:
        context = Ctx()
        def fill(self, *a): pass
        def wait_for_load_state(self, *a, **k): pass
        def query_selector_all(self, sel): return []
    for n in ("_throttle", "dismiss_overlays", "_check_login", "_click"): monkeypatch.setattr(W, n, lambda *a, **k: None)
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(W, "detail_image", lambda dp, lid: calls.append(lid) or (IMG if lid == 630614482 else None))
    f = W.page_finder(Pg())
    assert f(630614482) == IMG and f(4999999999) is None and calls == [630614482, 4999999999]
    assert len(Pg.context.pages) == 1                                    # 详情页标签复用
    f.cleanup(); assert Pg.context.pages[0].closed
    monkeypatch.setattr(W, "DETAIL_FALLBACK", False); calls.clear()
    assert W.page_finder(Pg())(630614482) is None and calls == []


def test_launch_ctx_falls_back_to_system_chrome(monkeypatch):
    seen = []
    class Chromium:
        def launch_persistent_context(self, profile, **kw):
            seen.append(kw.get("channel"))
            if "channel" not in kw: raise RuntimeError("BrowserType.launch_persistent_context: Executable doesn't exist at /x/chrome-headless-shell")
            return "ctx"
    class P: chromium = Chromium()
    monkeypatch.setattr(W.os.path, "exists", lambda p: True)
    assert W.launch_ctx(P()) == "ctx" and seen == [None, "chrome"]
    class Bad(Chromium):
        def launch_persistent_context(self, profile, **kw): raise RuntimeError("other failure")
    class P2: chromium = Bad()
    with pytest.raises(RuntimeError, match="other"): W.launch_ctx(P2())


def test_lookups_served_while_waiting_for_vision(monkeypatch):
    """列表读完只剩豆包长尾时，每 IDLE_LOOKUP_EVERY 秒穿插查询（2026-10 长豆包批次期间查询排队超时）。"""
    monkeypatch.setattr(W, "SEEN", {}); monkeypatch.setattr(W, "IDLE_LOOKUP_EVERY", 0.05)
    for n, v in (("dedupe", lambda rows: rows), ("guard", lambda o: None), ("rule", lambda t: None), ("reason_group", lambda r: r)): monkeypatch.setattr(W.C, n, v)
    monkeypatch.setattr(W, "log_exclusion", lambda *a, **k: None)
    gate = threading.Event()
    def judge(o):
        gate.wait(5)
        return ({**o, "结果": "符合", "原因": "", "price_min": 1, "price_max": 1, "total_sales": 1, "sales_7d": 1}, {}, 3)
    monkeypatch.setattr(W, "classify_parallel", judge)
    idles = []
    def idle():
        idles.append(1)
        if len(idles) >= 3: gate.set()
    rows = [{"rank": i, "page": 1, "listing_id": str(100 + i), "title": f"t{i}", "image_url": ""} for i in range(1, 4)]
    st = RunStore()
    assert W.process_batch(st, {"id": "r", "user_id": "u", "keyword": "k"}, {"id": "b", "batch_no": 1, "offset_end": 3}, iter(rows), idle=idle) == "done"
    assert len(idles) >= 3 and len(st.items) == 3


def test_heartbeat_thread_writes(monkeypatch):
    got = []
    class S:
        def heartbeat(self, info): got.append(info)
    W.start_heartbeat(S(), every=0.05); time.sleep(0.2)
    assert got and got[0]["phase"] in ("idle", "lookups", "batch", "counting") and "pid" in got[0]


class HBStore(LookupStore):
    def __init__(self, hb_age=None):
        super().__init__(); self.hb = None if hb_age is None else {"last_seen": (_now() - timedelta(seconds=hb_age)).isoformat()}
    def box_heartbeat(self): return self.hb


def _stale(st, minutes):
    st.results[6] = {"status": "queued", "requested_at": (_now() - timedelta(minutes=minutes)).isoformat()}


def test_ecs_busy_box_keeps_waiting_offline_box_trips():
    st = HBStore(hb_age=20); _stale(st, 15)
    with pytest.raises(EHuntWebPending, match="box 在线但正忙"): prov(st, timeout=0).get_listing(6)
    _stale(st, 45)
    with pytest.raises(EHuntServiceError, match="在线但请求排队超过 30 分钟"): prov(st, timeout=0).get_listing(6)
    st = HBStore(hb_age=3 * 3600); _stale(st, 15)
    with pytest.raises(EHuntServiceError, match="离线：最后心跳 180 分钟前"): prov(st, timeout=0).get_listing(6)
    st = HBStore(); _stale(st, 15)                                        # 没有心跳行（旧 box worker）→ 与以前相同
    with pytest.raises(EHuntServiceError, match="box 取数 worker 未响应"): prov(st, timeout=0).get_listing(6)
