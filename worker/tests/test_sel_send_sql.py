"""039 sel_send_to_screen：静态检查 + （box 上有 Postgres 时）跑本地一次性实例测试。"""
import re, shutil, subprocess, glob
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
SQL = (ROOT / "supabase/migrations/039_sel_send_to_screen.sql").read_text()


def test_static():
    f = re.search(r"create or replace function public\.sel_send_to_screen.*?end \$\$;", SQL, re.S).group(0)
    assert "security definer" in f and "sb.user_id = uid" in f and "pg_advisory_xact_lock" in f and "for update of sb" in f
    assert "k >= 500" in f and "1000 - count(*)" in f and "status in ('done','canceled','failed')" in f
    assert "sent_count = done0 + n" in f and "raise exception" in f   # 额度为 0 → 整体回滚，不留空批次
    assert "revoke all on function public.sel_send_to_screen(uuid[], boolean) from public, anon" in SQL


@pytest.mark.skipif(not glob.glob("/usr/lib/postgresql/*/bin/initdb") or not shutil.which("psql"), reason="no local postgres")
def test_local_postgres():
    out = subprocess.run(["bash", str(ROOT / "supabase/tests/run_039_local.sh")], capture_output=True, text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "PORT": "55441"})
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert "ALL 039 TESTS PASSED" in out.stdout and "并发请求只建了 1 份" in out.stdout
