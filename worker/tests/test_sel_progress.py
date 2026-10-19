"""034 获取链接实时进度：Progress 节流/阶段切换/迁移未执行时停写；process_batch 写出的读/判定/通过计数。"""
import io, sys, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


class RunStore:
    def __init__(self, fail=None): self.writes, self.batches, self.items, self.fail, self.status = [], [], [], fail, "running"
    def patch_run(self, rid, **kw):
        if "progress" in kw:
            if self.fail: raise self.fail
            self.writes.append(dict(kw["progress"]))
    def patch_batch(self, bid, **kw): self.batches.append(kw)
    def passed_ids(self, rid): return set()
    def insert_item(self, body): self.items.append(body)
    def run(self, rid): return {"id": rid, "status": self.status, "max_rows": 10, "user_id": "u"}


def test_progress_throttles_within_stage_but_writes_stage_changes(monkeypatch):
    monkeypatch.setattr(W, "PROGRESS_ON", [True])
    st, ck = RunStore(), Clock(); p = W.Progress(st, "r", min_interval=2, clock=ck)
    assert p.update(stage="reading", read=1)           # 首次立即写
    ck.t = 0.5; assert not p.update(read=2)             # 同阶段 2 秒内不写
    ck.t = 1.0; assert p.update(stage="vision", read=3) # 阶段切换立即写
    ck.t = 1.5; assert not p.update(read=4)
    ck.t = 3.1; assert p.update(read=5)
    assert [w["read"] for w in st.writes] == [1, 3, 5] and all(w.get("at") for w in st.writes)
    assert st.writes[-1]["stage"] == "vision"           # 未写出的字段也被合并进下一次
    assert p.update(force=True) and st.writes[-1]["read"] == 5


def test_progress_disables_itself_when_column_missing(monkeypatch):
    monkeypatch.setattr(W, "PROGRESS_ON", [True])
    err = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"code":"PGRST204"}'))
    st = RunStore(fail=err); p = W.Progress(st, "r", min_interval=0)
    assert not p.update(stage="reading") and W.PROGRESS_ON == [False]
    st.fail = None; assert not p.update(stage="vision") and st.writes == []   # 本进程不再尝试


def test_process_batch_reports_read_judged_passed(monkeypatch):
    monkeypatch.setattr(W, "PROGRESS_ON", [True]); monkeypatch.setattr(W, "SEEN", {})
    monkeypatch.setattr(W.C, "dedupe", lambda rows: rows)
    monkeypatch.setattr(W.C, "guard", lambda o: None)
    monkeypatch.setattr(W.C, "rule", lambda t: ("排除", "定制", "关键词:custom") if "custom" in t else None)
    monkeypatch.setattr(W.C, "reason_group", lambda r: r)
    def judge(o):
        ok = int(o["listing_id"]) % 2 == 0
        return ({**o, "结果": "符合" if ok else "排除", "原因": "" if ok else "材质", "price_min": 10, "price_max": 10, "total_sales": 100, "sales_7d": 5}, {}, 3)
    monkeypatch.setattr(W, "classify_parallel", judge)
    monkeypatch.setattr(W, "log_exclusion", lambda *a, **k: None)
    rows = [{"rank": i, "page": 1, "listing_id": str(100 + i), "title": "custom tag" if i == 3 else f"item {i}", "image_url": ""} for i in range(1, 11)]
    st = RunStore(); W._PROG[0] = W.Progress(st, "r", min_interval=0)
    W._PROG[0].update(stage="read_setup", target=10, read=0)
    try:
        assert W.process_batch(st, {"id": "r", "user_id": "u", "keyword": "k"}, {"id": "b", "batch_no": 1, "offset_end": 10}, iter(rows)) == "done"
    finally: W._PROG[0] = None
    last = st.writes[-1]
    assert last["stage"] == "done" and last["read"] == 10 and last["judged"] == 10 and last["target"] == 10
    assert last["vision_pending"] == 0 and last["excluded"] == 1 + 4 and last["passed"] == 5 == len(st.items)
    stages = [w["stage"] for w in st.writes]
    assert "reading" in stages and "vision" in stages and stages.index("reading") < stages.index("vision") < stages.index("done")
    reads = [w["read"] for w in st.writes]; assert reads == sorted(reads)           # 单调递增
