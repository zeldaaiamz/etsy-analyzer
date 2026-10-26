"""052 查询数量成功后自动入队：迁移 SQL 静态检查。"""
import re
from pathlib import Path

SQL = (Path(__file__).resolve().parents[2] / "supabase/migrations/052_sel_auto_start.sql").read_text()

def fn(name):
    m = re.search(rf"create or replace function public\.{name}\(.*?end \$\$;", SQL, re.S)
    assert m, name
    return m.group(0)

def test_auto_limit_documented():
    assert "DEFAULT_AUTO_LIMIT = 5000" in SQL
    assert "least(new.total_count, 5000)" in SQL
    assert "5000" in fn("sel_start_core")

def test_no_concurrent_block():
    assert "raise exception" not in fn("sel_start").lower() or "进行中" not in fn("sel_start")
    body = fn("sel_start") + fn("sel_start_core")
    assert "raise exception" not in body.lower() or all(
        "进行中" not in line for line in body.splitlines() if "raise" in line.lower()
    )
    assert "status in ('running'" not in fn("sel_start")
    assert "status in ('running'" not in fn("sel_start_core")

def test_trigger_on_counted():
    assert "sel_runs_auto_start" in SQL
    assert "new.status = 'counted'" in SQL
    assert "coalesce(new.total_count, 0) > 0" in SQL
    assert "total_count=0" in SQL  # comment: 0 不入队

def test_sel_start_uses_core():
    assert "sel_start_core" in fn("sel_start")
    assert "queue_at = now()" in fn("sel_start_core")
