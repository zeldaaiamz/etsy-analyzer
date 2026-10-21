"""049 产品库评论日期自动抓取 worker（selection/review_scraper.py）+ 049 迁移本地 Postgres 测试。"""
import datetime as dt, glob, shutil, subprocess
from pathlib import Path
import pytest
from selection import review_scraper as R

ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date(2026, 10, 21)


def ts(d): return int(dt.datetime(d.year, d.month, d.day, 12, tzinfo=dt.timezone.utc).timestamp())


class FakeStore:
    def __init__(self, pending, busy=False):
        self.pending, self.busy, self.done, self.claims = list(pending), busy, {}, 0
    def claim(self, n):
        self.claims += 1; got, self.pending = self.pending[:n], self.pending[n:]; return got
    def finish(self, lid, status, dates=None, source="etsy_page", error=None): self.done[lid] = (status, list(dates or []), source, error)
    def ehunt_busy(self): return self.busy


def test_api_fetcher_paginates_and_cuts_old():
    newest = [TODAY - dt.timedelta(days=i) for i in range(100)]          # 第 1 页 100 条（满页 → 继续）
    page2 = [TODAY - dt.timedelta(days=150), TODAY - dt.timedelta(days=400)]   # 第 2 页出现超出窗口的 → 停
    calls = []
    def get(url, h):
        calls.append(url); assert h == {"x-api-key": "k"}
        rows = newest if "offset=0" in url else page2
        return {"count": 500, "results": [{"create_timestamp": ts(d), "review": ""} for d in rows]}
    out = R.ApiFetcher("k", get=get, today=TODAY)(123)
    assert len(calls) == 2 and "/listings/123/reviews" in calls[0] and "limit=100" in calls[0]
    assert len(out) == 101 and out[0] == TODAY and TODAY - dt.timedelta(days=400) not in out   # 空文字评论也计入日期
    assert out == sorted(out, reverse=True)


def test_api_fetcher_single_short_page():
    out = R.ApiFetcher("k", get=lambda u, h: {"count": 2, "results": [{"create_timestamp": ts(TODAY)}, {"create_timestamp": ts(TODAY)}]}, today=TODAY)(1)
    assert out == [TODAY, TODAY]


def test_page_dates_and_block_detection():
    txt = "Reviews for this item (133)\nSally Oct 13, 2026\nGreat!\nBob Sep 30, 2026\n小明 2026年10月2日"
    assert R.page_dates(txt, TODAY) == [dt.date(2026, 10, 13), dt.date(2026, 9, 30), dt.date(2026, 10, 2)]
    assert R.is_blocked(403, "") and R.is_blocked(200, "<script src='https://ct.captcha-delivery.com/c.js'>") and not R.is_blocked(200, "<html>ok</html>")


def test_run_once_ok_and_failed_never_stuck(monkeypatch):
    s = FakeStore([1, 2, 3, 4])
    def fetch(lid):
        if lid == 2: raise R.FetchError("Etsy 反爬拦截（HTTP 403）")
        if lid == 3: raise ValueError("boom")
        return [TODAY]
    fetch.source, fetch.uses_browser = "etsy_page", True
    sleeps = []; monkeypatch.setattr(R, "_sleep", sleeps.append)
    assert R.run_once(s, fetch, batch=3, gap=8) == 3
    assert s.done[1] == ("ok", [TODAY], "etsy_page", None)
    assert s.done[2][0] == "failed" and "反爬" in s.done[2][3]
    assert s.done[3][0] == "failed" and s.done[3][3].startswith("ValueError")   # 任何异常都写 failed，不留在 fetching
    assert 4 not in s.done and s.pending == [4]                                   # 小批量
    assert sleeps == [8, 8]                                                        # 每个之间间隔


def test_browser_yields_to_ehunt_queue():
    s = FakeStore([1], busy=True)
    f = lambda lid: [TODAY]; f.source, f.uses_browser = "etsy_page", True
    assert R.run_once(s, f) == 0 and s.claims == 0 and not s.done       # EHunt 忙 → 不认领
    g = lambda lid: [TODAY]; g.source, g.uses_browser = "etsy_api", False
    assert R.run_once(s, g, gap=0) == 1 and s.done[1][2] == "etsy_api"   # API 不占浏览器 → 不需要让路


def test_make_fetcher(monkeypatch):
    monkeypatch.setenv("ETSY_API_KEY", "x"); assert isinstance(R.make_fetcher(), R.ApiFetcher)
    monkeypatch.setenv("ETSY_API_KEY", ""); assert isinstance(R.make_fetcher(), R.BrowserFetcher)


def test_dry_run_touches_no_db(monkeypatch, capsys):
    f = lambda lid: [TODAY]; f.source = "etsy_page"
    monkeypatch.setattr(R, "make_fetcher", lambda: f)
    monkeypatch.setattr(R, "Store", lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run must not touch DB")))
    assert R.main(["--dry-run", "42"]) == 0 and "42: ok via etsy_page, 1 dates" in capsys.readouterr().out


def test_separate_process_and_profile():
    src = (ROOT / "worker/selection/review_scraper.py").read_text()
    assert "ehunt_worker" not in src.split('"""', 2)[2]          # 不 import EHunt worker（独立进程，不阻塞其队列）
    assert R.PROFILE.endswith("reviews-profile")


SQL = (ROOT / "supabase/migrations/049_auto_reviews.sql").read_text()
def test_migration_static():
    assert "after insert on public.product_library" in SQL and "on conflict (listing_id) do nothing" in SQL
    assert "for update skip locked" in SQL and "除了失败重试，不重复抓取" in SQL


@pytest.mark.skipif(not glob.glob("/usr/lib/postgresql/*/bin/initdb") or not shutil.which("psql"), reason="no local postgres")
def test_migration_local_postgres():
    out = subprocess.run(["bash", str(ROOT / "supabase/tests/run_049_local.sh")], capture_output=True, text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "PORT": "55449"})
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert "ALL 049 TESTS PASSED" in out.stdout
