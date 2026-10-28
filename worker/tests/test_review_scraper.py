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
        self.pending, self.busy, self.done, self.claims, self.requeued, self.beats = list(pending), busy, {}, 0, [], []
    def claim(self, n):
        self.claims += 1; got, self.pending = self.pending[:n], self.pending[n:]; return got
    def finish(self, lid, status, dates=None, source="etsy_page", error=None): self.done[lid] = (status, list(dates or []), source, error)
    def ehunt_busy(self): return self.busy
    def requeue(self, lid): self.requeued.append(lid)
    def heartbeat(self, info=None): self.beats.append(info or {})


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


def test_browser_does_not_yield_to_ehunt_by_default(monkeypatch):
    """默认评论抓取优先：EHunt/选品忙时仍认领并抓取。"""
    monkeypatch.setattr(R, "YIELD_TO_EHUNT", False)
    s = FakeStore([1], busy=True)
    f = lambda lid: [TODAY]; f.source, f.uses_browser = "etsy_page", True
    assert R.run_once(s, f, gap=0) == 1 and s.claims == 1 and s.done[1][0] == "ok"


def test_browser_yields_to_ehunt_when_env_on(monkeypatch):
    """REVIEW_YIELD_TO_EHUNT=1 时恢复旧让路：浏览器模式 EHunt 忙 → 不认领；API 模式不让路。"""
    monkeypatch.setattr(R, "YIELD_TO_EHUNT", True)
    s = FakeStore([1], busy=True)
    f = lambda lid: [TODAY]; f.source, f.uses_browser = "etsy_page", True
    assert R.run_once(s, f) == 0 and s.claims == 0 and not s.done
    g = lambda lid: [TODAY]; g.source, g.uses_browser = "etsy_api", False
    assert R.run_once(s, g, gap=0) == 1 and s.done[1][2] == "etsy_api"


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
    assert R.YIELD_TO_EHUNT is False                            # 默认不让路：评论抓取优先
    assert "YIELD_TO_EHUNT and" in src                          # 让路仅在显式开启时


SQL = (ROOT / "supabase/migrations/049_auto_reviews.sql").read_text()
def test_migration_static():
    assert "after insert on public.product_library" in SQL and "on conflict (listing_id) do nothing" in SQL
    assert "for update skip locked" in SQL and "除了失败重试，不重复抓取" in SQL


@pytest.mark.skipif(not glob.glob("/usr/lib/postgresql/*/bin/initdb") or not shutil.which("psql"), reason="no local postgres")
def test_migration_local_postgres():
    out = subprocess.run(["bash", str(ROOT / "supabase/tests/run_049_local.sh")], capture_output=True, text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "PORT": "55449"})
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert "ALL 049 TESTS PASSED" in out.stdout


SQL054 = (ROOT / "supabase/migrations/054_manual_reviews.sql").read_text()
def test_migration_054_static():
    assert "queue_library_review_scrapes" in SQL054 and "skipped_ok" in SQL054
    assert "status = 'failed'" in SQL054
    body = "\n".join(l for l in SQL054.splitlines() if not l.strip().startswith("--"))
    assert "sel_keyword_log" not in body

def test_reviews_heartbeat_box_reviews():
    src = (ROOT / "worker/selection/review_scraper.py").read_text()
    assert "box-reviews" in src and "def heartbeat" in src
    # run_once 每轮先心跳，便于产品库判断进程是否在跑；冷却时 info 带 cool_until
    assert "cool_heartbeat_info" in src and "cool_until" in src and "cool_remaining_s" in src
    assert "getattr(store, \"heartbeat\"" in src or "store.heartbeat" in src or "beat(cool_heartbeat_info" in src

@pytest.mark.skipif(not glob.glob("/usr/lib/postgresql/*/bin/initdb") or not shutil.which("psql"), reason="no local postgres")
def test_migration_054_local_postgres():
    out = subprocess.run(["bash", str(ROOT / "supabase/tests/run_054_local.sh")], capture_output=True, text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "PORT": "55454"})
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert "ALL 054 TESTS PASSED" in out.stdout


# ---------------- 网页抓取（有头 Chrome）：read_listing / BrowserFetcher 节流 / 显示 ----------------
def test_heading_kind_and_counts():
    assert R.heading_kind("Reviews for this item (80)") == ("item", 80)
    assert R.heading_kind("Reviews for this item (1k)") == ("item", 1000)
    assert R.heading_kind("Reviews for this item (1,234)") == ("item", 1234)
    assert R.heading_kind("Reviews for this shop (146)") == ("shop", 146)
    assert R.heading_kind("Something else") == (None, None)


class FakeNav:
    def __init__(self, pages, status=200, header="Reviews for this item (80)", sort=True, html="<html>", view_all=True, shop=True):
        self.pages, self.status, self.header, self.sort, self._html, self.view_all, self.shop = pages, status, header, sort, html, view_all, shop
        self.i, self.views, self.warm, self.sorted = 0, 0, 0, False
    def warm_up(self): self.warm += 1; self.views += 1
    def open_listing(self, lid): self.views += 1; self.i = 0; return self.status
    def html(self): return self._html
    def shop_count(self): return 146
    def has_shop_reviews(self): return self.shop
    def open_item_reviews(self): return self.header if self.view_all else None
    def sort_recent(self): self.sorted = True; return self.sort
    def page_dates(self, today=None): return self.pages[self.i]
    def next_page(self):
        if self.i + 1 >= len(self.pages): return False
        self.i += 1; self.views += 1; return True


D = lambda n: TODAY - dt.timedelta(days=n)
def test_read_listing_most_recent_stops_at_200_days():
    nav = FakeNav([[D(1), D(5), D(9)], [D(30), D(150), D(199), D(201)], [D(300)]])
    out, info = R.read_listing(nav, 1, today=TODAY)
    assert nav.sorted and out == [D(1), D(5), D(9), D(30), D(150), D(199)] and info["pages"] == 2 and nav.i == 1   # 遇到 >200 天就停，不翻第 3 页
    assert info["item_count"] == 80 and info["header_count"] == 146 and "truncated" not in info


def test_read_listing_last_page_and_truncation():
    out, info = R.read_listing(FakeNav([[D(1)], [D(2)]]), 1, today=TODAY); assert out == [D(1), D(2)] and info["pages"] == 2
    out, info = R.read_listing(FakeNav([[D(i)] for i in range(1, 30)]), 1, today=TODAY, max_pages=5)
    assert len(out) == 5 and info["truncated"]


def test_read_listing_item_only_and_errors():
    with pytest.raises(R.FetchError, match="仅店铺评论"): R.read_listing(FakeNav([[]], header="Reviews for this shop (146)"), 1, today=TODAY)
    with pytest.raises(R.FetchError, match="仅店铺评论"): R.read_listing(FakeNav([[]], view_all=False), 1, today=TODAY)
    with pytest.raises(R.FetchError, match="暂无评论"): R.read_listing(FakeNav([[]], view_all=False, shop=False), 1, today=TODAY)
    with pytest.raises(R.FetchError, match="Most recent"): R.read_listing(FakeNav([[]], sort=False), 1, today=TODAY)
    with pytest.raises(R.FetchError, match="404"): R.read_listing(FakeNav([[]], status=404), 1, today=TODAY)
    with pytest.raises(R.Blocked): R.read_listing(FakeNav([[]], status=403), 1, today=TODAY)
    with pytest.raises(R.Blocked): R.read_listing(FakeNav([[]], html="<script src=//ct.captcha-delivery.com>"), 1, today=TODAY)


class Clock:
    def __init__(self): self.t = 1_800_000_000.0
    def __call__(self): return self.t


def test_browser_fetcher_warmup_block_pause_and_requeue(monkeypatch):
    navs = []
    def factory():
        n = FakeNav([[TODAY]]); navs.append(n); return n
    c = Clock(); f = R.BrowserFetcher(clock=c, nav_factory=factory)
    assert f.ready() == (True, "") and f(1) == [TODAY] and navs[0].warm == 1          # 会话开始先预热首页
    navs[0].status = 403
    with pytest.raises(R.Blocked): f(2)
    ok, why = f.ready(); assert not ok and "反爬" in why and f.nav is None               # 暂停 30–60 分钟，浏览器已关
    assert 30 * 60 <= f.pause_until - c.t <= 60 * 60
    # 暂停中 run_once 不认领
    s = FakeStore([5]); s.requeue = lambda lid: None
    assert R.run_once(s, f) == 0 and s.claims == 0
    c.t = f.pause_until + 1; assert f.ready()[0]
    assert f(3) == [TODAY] and len(navs) == 2 and navs[1].warm == 1                     # 恢复后重开浏览器并重新预热


def test_run_once_requeues_rest_when_paused_mid_batch(monkeypatch):
    c = Clock(); n = FakeNav([[TODAY]], status=403); f = R.BrowserFetcher(clock=c, nav_factory=lambda: n)
    s = FakeStore([1, 2, 3]); req = []; s.requeue = req.append
    monkeypatch.setattr(R, "_sleep", lambda x: None)
    assert R.run_once(s, f, batch=3, gap=0) == 3
    assert s.done[1][0] == "failed" and "反爬" in s.done[1][3] and req == [2, 3] and 2 not in s.done   # 第 1 个被拦截 → 其余放回 pending


def test_session_page_budget_cools_down(monkeypatch):
    monkeypatch.setattr(R, "SESSION_PAGES", 6)
    c = Clock(); f = R.BrowserFetcher(clock=c, nav_factory=lambda: FakeNav([[TODAY], [TODAY], [TODAY]]))
    f(1)   # 预热 1 + 商品页 1 + 翻页 2 = 4
    assert f.ready()[0]
    f(2)   # 再 3 → 7 ≥ 6 → 冷却
    ok, why = f.ready(); assert not ok and "冷却" in why and 20 * 60 <= f.pause_until - c.t <= 30 * 60


def test_cool_heartbeat_info_while_paused():
    c = Clock(); f = R.BrowserFetcher(clock=c, nav_factory=lambda: FakeNav([[TODAY]]))
    f.pause(1500, "本会话已看 30 页，冷却")
    info = R.cool_heartbeat_info(f, pid=4242)
    assert info["pid"] == 4242 and info["cool_remaining_s"] == 1500
    assert info["cool_until"].endswith("Z") and "冷却" in info["cool_reason"]
    # 未暂停 → 只有 pid
    c.t = f.pause_until + 1
    assert R.cool_heartbeat_info(f, pid=1) == {"pid": 1}
    # 无 pause_until 的 fetcher
    class Plain: pass
    assert R.cool_heartbeat_info(Plain(), pid=9) == {"pid": 9}


def test_run_once_writes_cool_into_heartbeat_when_cooling(monkeypatch):
    c = Clock(); f = R.BrowserFetcher(clock=c, nav_factory=lambda: FakeNav([[TODAY]]))
    f.pause(23 * 60, "本会话已看 30 页，冷却")
    s = FakeStore([1])
    assert R.run_once(s, f) == 0 and s.claims == 0
    assert s.beats, "应写心跳"
    # ready() 前 + 跳过时各一次，都带 cool_*
    assert all(b.get("cool_remaining_s") == 23 * 60 for b in s.beats)
    assert all(b.get("cool_until") for b in s.beats)
    assert all("冷却" in (b.get("cool_reason") or "") for b in s.beats)


def test_warm_every_10_and_daily_cap(monkeypatch):
    monkeypatch.setattr(R, "DAILY_CAP", 12)
    n = FakeNav([[TODAY]]); c = Clock(); f = R.BrowserFetcher(clock=c, nav_factory=lambda: n)
    for i in range(12): assert f.ready()[0]; f(i)
    assert n.warm == 2                                    # 第 1 个前 + 第 10 个之后
    ok, why = f.ready(); assert not ok and "上限" in why
    c.t += 86400; assert f.ready()[0]                     # 第二天清零


def test_display_missing_no_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "DISPLAY", ":93"); monkeypatch.setattr(R, "START_XVFB", False)
    f = R.BrowserFetcher(clock=Clock())
    ok, why = f.ready(); assert not ok and ":93" in why and f.pause_until > 0     # 不认领、暂停 10 分钟
    s = FakeStore([1]); assert R.run_once(s, f) == 0 and s.claims == 0
    f2 = R.BrowserFetcher(clock=Clock()); f2.pause_until = 0
    with pytest.raises(R.DisplayUnavailable): f2(1)                                # 中途丢失 → 本条 failed（run_once 写）+ 暂停
    assert f2.pause_until > 0


def test_ensure_display_spawns_xvfb(monkeypatch):
    calls = []; exists = {"v": False}
    monkeypatch.setattr(R, "display_ok", lambda d: exists["v"])
    monkeypatch.setattr(R, "_sleep", lambda s: None)
    def spawn(cmd, **kw): calls.append(cmd); exists["v"] = True
    assert R.ensure_display(":77", True, spawn=spawn) and calls[0][:2] == ["Xvfb", ":77"]
    exists["v"] = False
    assert not R.ensure_display(":77", False, spawn=spawn) and len(calls) == 1
    def bad(cmd, **kw): raise OSError("no Xvfb")
    assert not R.ensure_display(":77", True, spawn=bad)


def test_ehunt_busy_ignores_stale_queue(monkeypatch):
    st = R.Store("http://x", "k"); seen = []
    monkeypatch.setattr(st, "_req", lambda m, path, *a, **k: seen.append(path) or [])
    assert st.ehunt_busy(now=1_800_000_000) is False
    assert all("gte.2027-01-15T07%3A45%3A00Z" in p for p in seen) and "heartbeat_at" in seen[0] and "requested_at" in seen[1]


def test_empty_first_page_is_failure_not_zero_and_shop_break():
    with pytest.raises(R.FetchError, match="没读到日期"): R.read_listing(FakeNav([[]]), 1, today=TODAY)
    out, _ = R.read_listing(FakeNav([[D(300), D(400)]]), 1, today=TODAY); assert out == []      # 全部超过 200 天 → 合法的 0 条
    n = FakeNav([[]], view_all=False, shop=False, html="<p>This shop is taking a short break.</p>")
    with pytest.raises(R.FetchError, match="暂停营业"): R.read_listing(n, 1, today=TODAY)


def test_unsorted_page_means_sort_failed():
    with pytest.raises(R.FetchError, match="排序未生效"): R.read_listing(FakeNav([[D(1), D(300), D(5)]]), 1, today=TODAY)
    _, info = R.read_listing(FakeNav([[D(1), D(1), D(5)]]), 1, today=TODAY); assert info["first_page"][0] == D(1).isoformat()


# ---------- 056：产品不可售 ----------
class UnavailNav(FakeNav):
    def __init__(self, title="", body="", **kw):
        super().__init__([[]], view_all=False, shop=False, **kw); self._title, self._body = title, body
    def title(self): return self._title
    def body_text(self): return self._body


def test_unavailable_reason_real_pages():
    # 2026-10-28 真实页面（headed Chrome）：标题 / 正文片段
    assert "下架" in R.unavailable_reason("This item is unavailable - Etsy", "Sorry, this item is unavailable. Similar items on Etsy")   # 1332654646
    assert "售罄" in R.unavailable_reason("Do Not Disturb Kindle Grip - Etsy", "Gift Cards Sorry, this item is sold out bookshopbykendra")  # 4363080306
    assert "暂停营业" in R.unavailable_reason("Surprise Pack - Etsy", "Gift Cards This shop is taking a short break. BloomwolfStudio")      # 500326663
    assert R.unavailable_reason("Kindle Grip - Etsy", "Add to cart  Reviews for this item (80)") is None
    assert R.unavailable_reason(None, None) is None


def test_read_listing_unavailable_paths():
    with pytest.raises(R.Unavailable, match="410"): R.read_listing(FakeNav([[]], status=410), 1, today=TODAY)          # 1439586162
    with pytest.raises(R.Unavailable, match="404"): R.read_listing(FakeNav([[]], status=404), 1, today=TODAY)
    with pytest.raises(R.Unavailable, match="下架"): R.read_listing(UnavailNav("This item is unavailable - Etsy"), 1, today=TODAY)
    with pytest.raises(R.Unavailable, match="售罄"): R.read_listing(UnavailNav("X - Etsy", "Sorry, this item is sold out"), 1, today=TODAY)
    with pytest.raises(R.Unavailable, match="暂停营业"): R.read_listing(UnavailNav("X - Etsy", "This shop is taking a short break."), 1, today=TODAY)
    # 在售但没评论按钮 → 仍是普通 failed（可重试 / 手动录入）
    with pytest.raises(R.FetchError, match="暂无评论区") as e: R.read_listing(UnavailNav("X - Etsy", "Add to cart"), 1, today=TODAY)
    assert not isinstance(e.value, R.Unavailable)
    with pytest.raises(R.Blocked): R.read_listing(FakeNav([[]], status=403), 1, today=TODAY)   # 拦截优先，不误判为不可售


def test_normal_path_never_reads_title_or_body():
    class Strict(FakeNav):
        def title(self): raise AssertionError("正常路径不应读标题")
        def body_text(self): raise AssertionError("正常路径不应读正文")
    out, _ = R.read_listing(Strict([[D(1), D(2)]]), 1, today=TODAY); assert out == [D(1), D(2)]


def test_run_once_writes_unavailable_and_store_fallback(monkeypatch):
    s = FakeStore([1, 2])
    def fetch(lid):
        if lid == 1: raise R.Unavailable("商品已下架（HTTP 410）")
        return [TODAY]
    fetch.source = "etsy_page"; monkeypatch.setattr(R, "_sleep", lambda *_: None)
    assert R.run_once(s, fetch, batch=2, gap=0) == 2
    assert s.done[1] == ("unavailable", [], "etsy_page", "商品已下架（HTTP 410）") and s.done[2][0] == "ok"
    # 056 未上线：RPC 400 → 退回 failed
    import urllib.error
    st = R.Store("http://x", "k"); calls = []
    def req(m, path, body=None, prefer=None):
        calls.append(body["p_status"])
        if body["p_status"] == "unavailable": raise urllib.error.HTTPError(path, 400, "bad", {}, None)
    monkeypatch.setattr(st, "_req", req)
    st.finish(9, "unavailable", None, "etsy_page", "商品已下架（HTTP 410）"); assert calls == ["unavailable", "failed"]


def test_api_fetcher_404_410_unavailable():
    import io, urllib.error
    def boom(code):
        def f(req, timeout=30): raise urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))
        return f
    for code in (404, 410):
        orig = R.urllib.request.urlopen; R.urllib.request.urlopen = boom(code)
        try:
            with pytest.raises(R.Unavailable): R._get_json("http://x", {})
        finally: R.urllib.request.urlopen = orig


SQL056 = (ROOT / "supabase/migrations/056_reviews_unavailable.sql").read_text()
def test_migration_056_static():
    body = "\n".join(l for l in SQL056.splitlines() if not l.strip().startswith("--"))
    assert "'unavailable'" in body and "skipped_unavailable" in body and "skipped_manual" in body
    assert "status = 'failed'" in body and "除了获取失败，不重复获取" in SQL056
    assert "sel_keyword_log" not in body and "claim_review_scrapes" not in body      # claim 不改：unavailable 永不自动领取
    assert not any(w in body.lower() for w in ("drop table", "truncate", "delete from public.listing_scrape_status"))


@pytest.mark.skipif(not glob.glob("/usr/lib/postgresql/*/bin/initdb") or not shutil.which("psql"), reason="no local postgres")
def test_migration_056_local_postgres():
    out = subprocess.run(["bash", str(ROOT / "supabase/tests/run_056_local.sh")], capture_output=True, text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "PORT": "55456"})
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert "ALL 056 TESTS PASSED" in out.stdout
