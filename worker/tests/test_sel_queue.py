"""037 抓取队列：暂停 = worker 停在断点；最后几行内暂停不会被自动排批覆盖；迁移 SQL 静态检查。"""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402
from tests.test_selscreen_qa import Store  # noqa: E402

SQL = (Path(__file__).resolve().parents[2] / "supabase/migrations/037_sel_queue.sql").read_text()


class PausedStore(Store):
    def __init__(self, status="paused", after=0): super().__init__(); self.st, self.after, self.n = status, after, 0
    def run(self, rid):
        self.n += 1; return {**super().run(rid), "status": self.st if self.n > self.after else "running"}


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "EXCL_DIR", str(tmp_path)); monkeypatch.setattr(W, "SEEN", {})
    monkeypatch.setattr(W.C, "guard", lambda o: ("排除", "7天销量≤3", "")); monkeypatch.setattr(W.C, "dedupe", lambda rows: rows)


def test_pause_stops_at_breakpoint(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    rows = [{"listing_id": 8000 + i, "rank": i, "page": 1, "title": "t"} for i in range(1, 60)]
    st = PausedStore(); b = {"id": "b1", "batch_no": 1, "offset_start": 1, "offset_end": 59}
    assert W.process_batch(st, st.run("r"), b, iter(rows)) == "paused"
    assert any(p.get("status") == "canceled" for p in st.batch_p)                    # sel_resume 从这个批次的断点续读
    assert not any(p.get("status") in ("succeeded", "queued", "batch_done") for p in st.run_p)


def test_pause_in_last_rows_keeps_paused_for_next_batch(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    rows = [{"listing_id": 9000 + i, "rank": i, "page": 1, "title": "t"} for i in range(1, 6)]   # <20 行：循环内不检查
    st = PausedStore(after=2); b = {"id": "b1", "batch_no": 1, "offset_start": 1, "offset_end": 5}
    assert W.process_batch(st, st.run("r"), b, iter(rows)) == "done"
    assert st.run_p[-1]["status"] == "paused" and st.run_p[-1]["current_batch"] == 2


def test_cancel_still_cancels(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    rows = [{"listing_id": 7000 + i, "rank": i, "page": 1, "title": "t"} for i in range(1, 30)]
    st = PausedStore("canceled"); b = {"id": "b1", "batch_no": 1, "offset_start": 1, "offset_end": 29}
    assert W.process_batch(st, st.run("r"), b, iter(rows)) == "canceled"


def fn(name):
    m = re.search(rf"create or replace function public\.{name}\(.*?end \$\$;", SQL, re.S); assert m, name; return m.group(0)


def test_sql_queue_semantics():
    assert "'paused'" in SQL.split("sel_runs_status_check")[2]
    assert "已有进行中的任务" not in SQL                                # 多个任务可同时排队
    assert "count_queued','counting')) then raise" in fn("sel_count")   # 只防连点查询
    assert "order by case when status in ('running','counting') then 0 when status = 'count_queued' then 1 else 2 end, coalesce(queue_at, created_at)" in fn("sel_claim_run")
    assert "queue_at = now()" in fn("sel_start") and "queue_at = now()" in fn("sel_extend")
    assert "status in ('queued','running')" in fn("sel_pause")
    r = fn("sel_resume"); assert "'paused'" in r and "'failed'" in r and "interval '10 minutes'" in r and "delete" not in r  # 刷新=原地续读
    assert "'paused'" in fn("sel_cancel")
    d = fn("sel_delete_run"); assert "user_id = (select auth.uid())" in d and "请先停止" in d and "'running','paused'" in d
    assert "for update" in fn("sel_queue_move") and "pg_advisory_xact_lock" in fn("sel_queue_move")
    for f in ("sel_pause(uuid)", "sel_queue_move(uuid, int)", "sel_delete_run(uuid)"):
        assert f"revoke all on function public.{f} from public, anon" in SQL and f"grant execute on function public.{f} to authenticated" in SQL
