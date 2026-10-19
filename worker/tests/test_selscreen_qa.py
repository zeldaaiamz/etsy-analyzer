"""获取链接 QA 2026-10-19：查询中被取消不改回、自动排下一批失败不把已完成批次改成“已停止”、409 视为成功、过宽排除词忽略。"""
import io, re, sys, urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402


def http_err(code): return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b"{}"))


def test_patch_run_if_filters_on_status(monkeypatch):
    st = W.Store.__new__(W.Store); calls = []
    monkeypatch.setattr(st, "_req", lambda m, p, b=None, prefer=None, **k: calls.append((m, p, b)))
    st.patch_run_if("r1", "counting", status="counted", total_count=5)
    assert calls == [("PATCH", "/rest/v1/sel_runs?id=eq.r1&status=eq.counting", {"status": "counted", "total_count": 5})]


def test_count_result_does_not_override_cancel(monkeypatch):
    seen = []
    class S:
        def patch_run_if(self, rid, expect, **kw): seen.append(("if", expect, kw.get("status")))
        def patch_run(self, rid, **kw): seen.append(("plain", None, kw.get("status")))
    monkeypatch.setattr(W, "count_ehunt", lambda k, p: 19)
    class A: fake = None
    W.handle_run(S(), {"id": "r", "status": "counting", "keyword": "k"}, A(), False)
    assert seen == [("if", "counting", "counted")]
    monkeypatch.setattr(W, "count_ehunt", lambda k, p: (_ for _ in ()).throw(RuntimeError("关键词未生效")))
    seen.clear(); W.handle_run(S(), {"id": "r", "status": "counting", "keyword": "k"}, A(), False)
    assert seen == [("if", "counting", "failed")]


def test_new_batch_409_is_success_other_errors_raise(monkeypatch):
    st = W.Store.__new__(W.Store)
    monkeypatch.setattr(st, "_req", lambda *a, **k: (_ for _ in ()).throw(http_err(409))); st.new_batch({})
    monkeypatch.setattr(st, "_req", lambda *a, **k: (_ for _ in ()).throw(http_err(400)))
    with pytest.raises(urllib.error.HTTPError): st.new_batch({})


class Store:
    def __init__(self, fail_new=None):
        self.fail_new, self.run_p, self.batch_p = fail_new, [], []
    def run(self, rid): return {"id": rid, "status": "running", "max_rows": 5000, "read_all": True, "batch_size": 1000, "user_id": "u"}
    def passed_ids(self, rid): return set()
    def patch_batch(self, bid, **kw): self.batch_p.append(kw)
    def patch_run(self, rid, **kw): self.run_p.append(kw)
    def insert_item(self, b): pass
    def new_batch(self, body):
        if self.fail_new: raise http_err(self.fail_new)


def test_auto_queue_failure_keeps_batch_done(monkeypatch, tmp_path):
    """先读 200 再一键读取全部 4,800：第 6 批违反 024 的 batch_no ≤ 5 → 原来外层把已完成的第 5 批改成“已停止”，继续本批会无限失败。"""
    monkeypatch.setattr(W, "EXCL_DIR", str(tmp_path)); monkeypatch.setattr(W, "SEEN", {})
    monkeypatch.setattr(W.C, "guard", lambda o: ("排除", "7天销量≤3", "")); monkeypatch.setattr(W.C, "dedupe", lambda rows: rows)
    rows = [{"listing_id": 7000 + i, "rank": 3200 + i, "page": 161, "title": "t"} for i in range(1, 6)]
    st = Store(fail_new=400); run = st.run("r"); b = {"id": "b5", "batch_no": 5, "offset_start": 3201, "offset_end": 3205}
    assert W.process_batch(st, run, b, iter(rows)) == "done"
    assert any(p.get("status") == "done" for p in st.batch_p) and not any(p.get("status") in ("canceled", "failed") for p in st.batch_p)
    last = st.run_p[-1]; assert last["status"] == "batch_done" and last["read_all"] is False and "第 6 批" in last["error"]
    st2 = Store(); W.SEEN.clear()
    assert W.process_batch(st2, run, b, iter(rows)) == "done" and st2.run_p[-1]["status"] == "queued"


def test_too_broad_exclude_words_are_ignored():
    C = W.C
    for w in ["a|", ".*", r"\w+", "|name", "(x)?"]: assert C.too_broad(w), w
    seeds = re.findall(r"\('(?:custom|ip)', '((?:[^']|'')*)'", (Path(__file__).resolve().parents[2] / "supabase/migrations/031_sel_exclude_words.sql").read_text())
    assert len(seeds) > 40 and not [w for w in seeds if C.too_broad(w)]
    try:
        assert C.set_words([{"category": "custom", "word": "a|", "enabled": True}, {"category": "custom", "word": "engraved", "enabled": True}]) == "table"
        assert C.rule("Acrylic sunflower sign") is None and C.rule("engraved acrylic sign")
    finally: C.set_words(None)
