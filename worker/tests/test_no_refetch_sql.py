"""042 除了获取失败，不重复获取：静态检查 + 本地 Postgres 一次性实例测试。"""
import re, shutil, subprocess, glob
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
SQL = (ROOT / "supabase/migrations/042_no_refetch.sql").read_text()


def test_static():
    assert "status::text not in ('failed', 'canceled')" in SQL and "jobs_user_listing_idx" in SQL
    req = re.search(r"function public\.ehunt_lookup_request.*?end \$\$;", SQL, re.S).group(0)
    assert "'done'" not in req.split("where", 1)[1].split("returning")[0]      # done 永不重新排队
    claim = re.search(r"function public\.ehunt_lookup_claim.*?end \$\$;", SQL, re.S).group(0)
    assert "where status = 'queued'" in claim and "status = 'error'" in claim
    assert "除了获取失败，不重复获取" in SQL


@pytest.mark.skipif(not glob.glob("/usr/lib/postgresql/*/bin/initdb") or not shutil.which("psql"), reason="no local postgres")
def test_local_postgres():
    out = subprocess.run(["bash", str(ROOT / "supabase/tests/run_042_local.sh")], capture_output=True, text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "PORT": "55443"})
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert "ALL 042 TESTS PASSED" in out.stdout
