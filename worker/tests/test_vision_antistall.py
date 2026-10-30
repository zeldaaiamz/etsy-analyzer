"""防卡（2026-10-30 men gifts 卡在「剩 16 条等豆包判断」）：豆包请求硬总时限 / 有限重试 / 熔断 / 单条超时不挡后面 / 等豆包阶段心跳 + 看门狗 / stale run 自动重新排队。
用本地假豆包（只 accept 不回包、慢慢吐字节、正常秒回）模拟，证明批次在时限内完成、正常流程不变慢。"""
import json, logging, socket, sys, threading, time
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402


class FakeArk:
    """mode: hang（接受连接、读完请求、永不回）/ trickle（每 0.2s 吐 1 字节响应头）/ ok（立即返回合法 JSON）"""
    def __init__(self, mode):
        self.mode, self.hits = mode, 0
        self.srv = socket.socket(); self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(64)
        self.url = f"http://127.0.0.1:{self.srv.getsockname()[1]}/api/v3/chat/completions"; self.stop = False
        threading.Thread(target=self._loop, daemon=True).start()
    def _loop(self):
        while not self.stop:
            try: c, _ = self.srv.accept()
            except OSError: return
            self.hits += 1; threading.Thread(target=self._serve, args=(c,), daemon=True).start()
    def _serve(self, c):
        try:
            c.settimeout(5); buf = b""
            while b"\r\n\r\n" not in buf: buf += c.recv(65536)
            if self.mode == "hang": time.sleep(30); return
            if self.mode == "trickle":
                for ch in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 1000: c.sendall(bytes([ch])); time.sleep(0.2)
                return
            content = json.dumps({"分类": "保留", "说明": "ok"}, ensure_ascii=False)
            body = json.dumps({"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 1}}).encode()
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        except OSError: pass
        finally:
            try: c.close()
            except OSError: pass
    def close(self): self.stop = True; self.srv.close()


@pytest.fixture
def ark(monkeypatch):
    made = []
    def make(mode, total=0.6, attempts=3):
        f = FakeArk(mode); made.append(f)
        monkeypatch.setattr(W.C, "URL", f.url); monkeypatch.setattr(W.C, "KEY", "test-not-a-key", raising=False)
        monkeypatch.setattr(W, "ARK_TOTAL_TIMEOUT", total); monkeypatch.setattr(W, "ARK_CONNECT_TIMEOUT", 0.5)
        monkeypatch.setattr(W, "ARK_ATTEMPTS", attempts); monkeypatch.setattr(W, "ARK_BACKOFF", 0.01); monkeypatch.setattr(W, "ARK_BACKOFF_CAP", 0.02)
        monkeypatch.setattr(W, "ARK_BREAK_AFTER", 100); W.ARK.reset()
        return f
    yield make
    for f in made: f.close()
    W.ARK.reset()


def test_post_hard_total_timeout_when_server_never_replies(ark):
    f = ark("hang"); t0 = time.monotonic()
    with pytest.raises(W.ArkTimeout): W._ark_post(f.url, b"{}", {"Content-Type": "application/json"}, connect_timeout=0.5, total_timeout=0.6)
    assert time.monotonic() - t0 < 1.5


def test_post_hard_total_timeout_even_if_server_trickles_bytes(ark):
    """旧实现 timeout=120 是“单次读”超时：服务端隔一会吐 1 字节就能无限拖。新实现到总时限强制关 socket。"""
    f = ark("trickle"); t0 = time.monotonic()
    with pytest.raises(W.ArkTimeout): W._ark_post(f.url, b"{}", {}, connect_timeout=0.5, total_timeout=0.8)
    assert time.monotonic() - t0 < 1.8


def test_vision_retries_3_times_then_pending_with_warnings(ark, caplog):
    f = ark("hang"); t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=W.log.name):
        j, usage = W._ark_vision("t", b"img")
    assert time.monotonic() - t0 < 3 * 0.6 + 1.5
    assert j["分类"] == "待确认" and j["原因"] == W.VISION_TIMEOUT_REASON and usage == {}
    assert f.hits == 3
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "ark vision attempt" in r.getMessage()]
    assert len(warns) == 3 and "3/3" in warns[-1].getMessage()
    assert W.ARK.snapshot()["vision_retry"] == 0          # 结束后清掉网页上的“重试中”


def test_vision_normal_path_is_fast_and_unchanged(ark):
    ark("ok"); t0 = time.monotonic()
    j, usage = W._ark_vision("t", b"img")
    assert j == {"分类": "保留", "说明": "ok"} and usage == {"total_tokens": 1}
    assert time.monotonic() - t0 < 0.5


def test_circuit_breaker_stops_calling_ark(ark, monkeypatch):
    f = ark("hang", total=0.3, attempts=3); monkeypatch.setattr(W, "ARK_BREAK_AFTER", 2); monkeypatch.setattr(W, "ARK_BREAK_SECONDS", 30)
    W._ark_vision("t", b"img")           # 第 2 次失败时熔断，第 3 次不发请求
    assert f.hits == 2 and W.ARK.is_open() and W.ARK.snapshot()["vision_breaker_s"] > 0
    t0 = time.monotonic(); j, _ = W._ark_vision("t", b"img")
    assert f.hits == 2 and j["原因"] == W.VISION_TIMEOUT_REASON and time.monotonic() - t0 < 0.5   # 熔断期间立即判待确认


def test_retry_snapshot_visible_while_retrying(ark):
    ark("hang", total=0.4, attempts=3); seen = []
    th = threading.Thread(target=W._ark_vision, args=("t", b"img")); th.start()
    for _ in range(40):
        seen.append(W.ARK.snapshot()["vision_retry"]); time.sleep(0.03)
    th.join(5)
    assert max(seen) >= 1 and W.ARK.snapshot()["vision_retry_max"] == 3


def test_classify_parallel_item_budget(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(W, "_ORIG_UP", lambda u: (u, b"img"))
    monkeypatch.setattr(W, "_ark_vision", lambda t, i: gate.wait(10))
    monkeypatch.setattr(W, "ITEM_VISION_BUDGET", 0.3)
    monkeypatch.setattr(W.C, "ARGS", type("A", (), {"runs": 3})(), raising=False)
    t0 = time.monotonic()
    with pytest.raises(W.VisionTimeout): W.classify_parallel({"title": "t", "image_url": "u"})
    assert time.monotonic() - t0 < 1.0; gate.set()


class RunStore:
    def __init__(self): self.writes, self.beats, self.batches, self.items = [], 0, [], []
    def patch_run(self, rid, **kw):
        if "progress" in kw: self.writes.append(dict(kw["progress"]))
        if "heartbeat_at" in kw: self.beats += 1
    def patch_batch(self, bid, **kw): self.batches.append(kw)
    def passed_ids(self, rid): return set()
    def insert_item(self, body): self.items.append(body)
    def run(self, rid): return {"id": rid, "status": "running", "max_rows": 10, "user_id": "u"}


def _setup(monkeypatch, judge):
    monkeypatch.setattr(W, "PROGRESS_ON", [True]); monkeypatch.setattr(W, "SEEN", {})
    monkeypatch.setattr(W.C, "dedupe", lambda rows: rows); monkeypatch.setattr(W.C, "guard", lambda o: None)
    monkeypatch.setattr(W.C, "rule", lambda t: None); monkeypatch.setattr(W.C, "reason_group", lambda r: r)
    monkeypatch.setattr(W, "classify_parallel", judge); monkeypatch.setattr(W, "log_exclusion", lambda *a, **k: None)
    # 真实 C.dedupe 会补 price_min/price_max/total_sales/sales_7d；这里直接给出
    rows = [{"rank": i, "page": 1, "listing_id": str(100 + i), "title": f"item {i}", "image_url": "", "price_min": 1, "price_max": 1, "total_sales": 1, "sales_7d": 1} for i in range(1, 11)]
    return rows

def _ok(o): return ({**o, "结果": "保留", "原因": "", "price_min": 1, "price_max": 1, "total_sales": 1, "sales_7d": 1}, {}, 3)


def test_hung_head_item_does_not_block_batch_watchdog(monkeypatch, caplog):
    """队首一条豆包永远不回（旧代码：整批卡住、心跳停）→ 看门狗到点判「待确认（视觉超时）」，批次在时限内完成，其余正常落定。"""
    gate = threading.Event()
    def judge(o):
        if o["listing_id"] == "101": gate.wait(30)
        return _ok(o)
    rows = _setup(monkeypatch, judge)
    monkeypatch.setattr(W, "IDLE_LOOKUP_EVERY", 0.05); monkeypatch.setattr(W, "VISION_BEAT_EVERY", 0.1); monkeypatch.setattr(W, "VISION_STAGE_WATCHDOG", 1.0)
    st = RunStore(); W._PROG[0] = W.Progress(st, "r", min_interval=0); t0 = time.monotonic()
    try:
        with caplog.at_level(logging.WARNING, logger=W.log.name):
            assert W.process_batch(st, {"id": "r", "user_id": "u", "keyword": "k"}, {"id": "b", "batch_no": 1, "offset_end": 10}, iter(rows)) == "done"
    finally: W._PROG[0] = None; gate.set()
    assert time.monotonic() - t0 < 3.0
    assert len(st.items) == 10
    hung = [i for i in st.items if i["etsy_listing_id"] == 101][0]
    assert hung["verdict"] == "待确认" and hung["reason"] == W.VISION_TIMEOUT_REASON and "视觉超时" in hung["note"]
    assert all(i["verdict"] == "保留" for i in st.items if i["etsy_listing_id"] != 101)
    assert st.beats >= 3                                        # 等豆包期间也在写 run 心跳
    assert any(w.get("vision_wait_s", 0) >= 0 and w["stage"] == "vision" for w in st.writes)
    assert any("vision watchdog fired" in r.getMessage() for r in caplog.records)


def test_vision_timeout_item_marked_pending_not_blocking(monkeypatch):
    """单条超时（classify_parallel 抛 VisionTimeout）→ 这一条待确认，不需要看门狗。"""
    def judge(o):
        if o["listing_id"] == "101": raise W.VisionTimeout("单条豆包判断超过 1 秒")
        return _ok(o)
    rows = _setup(monkeypatch, judge); st = RunStore()
    assert W.process_batch(st, {"id": "r", "user_id": "u", "keyword": "k"}, {"id": "b", "batch_no": 1, "offset_end": 10}, iter(rows)) == "done"
    hung = [i for i in st.items if i["etsy_listing_id"] == 101][0]
    assert hung["verdict"] == "待确认" and hung["reason"] == W.VISION_TIMEOUT_REASON


def test_normal_batch_not_slowed(monkeypatch):
    def judge(o): time.sleep(0.01); return _ok(o)
    rows = _setup(monkeypatch, judge); st = RunStore(); t0 = time.monotonic()
    assert W.process_batch(st, {"id": "r", "user_id": "u", "keyword": "k"}, {"id": "b", "batch_no": 1, "offset_end": 10}, iter(rows)) == "done"
    assert time.monotonic() - t0 < 0.5 and len(st.items) == 10


def test_requeue_stale_runs_query(monkeypatch):
    calls = []
    class S:
        def _req(self, method, path, body=None, prefer=None, retries=None, timeout=None):
            calls.append((method, path, body)); return [{"id": "x", "keyword": "k"}] if "running" in path else []
    n = W.requeue_stale_runs(S(), own_rid="me", now=1_000_000_000)
    assert n == 1 and len(calls) == 2
    (m1, p1, b1), (m2, p2, b2) = calls
    assert m1 == "PATCH" and "status=eq.running" in p1 and "heartbeat_at=lt.2001-09-09T01:31:40Z" in p1 and "id=neq.me" in p1 and "attempts=lt." in p1
    assert b1["status"] == "queued" and b1["locked_by"] is None and b1["progress"]["stage"] == "requeued"
    assert "status=eq.counting" in p2 and b2["status"] == "count_queued"
