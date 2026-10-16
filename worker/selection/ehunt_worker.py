"""获取链接 worker（按需）：认领 sel_runs → Playwright 已登录 EHunt 的浏览器逐条读列表页 → 每条立即分类 → 写 sel_items。
读页面 DOM，不调用 EHunt API。部署设计见 deploy/etsy-selection-worker.service 与 tools/etsy_selection/README.md（未部署）。

运行位置：本 box（不是 ECS）。自己的 Playwright 持久化目录，不连接/不复制桌面浏览器的 cookie 或 profile。
首次：python -m selection.ehunt_worker --login   # 在 box 显示器（DISPLAY，默认 :1）打开有头浏览器到 EHunt 登录页，人工登录后关闭窗口即保存
运行：python -m selection.ehunt_worker            # 常驻轮询（deploy/box-selection-worker.sh 用 nohup 启动）
      python -m selection.ehunt_worker --fake list.json --once   # 不开浏览器，用回放 JSON 模拟抓取（测试）
环境：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY、ARK_API_KEY（分类也在 box 上跑）、EHUNT_PROFILE_DIR（默认 ~/.local/share/etsy-selection/ehunt-profile）；可选 EHUNT_UI（界面选择器 JSON）、EHUNT_FILTERS（筛选值 JSON）
"""
import argparse, json, os, socket, sys, time, logging, urllib.request, urllib.parse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "etsy_selection"))
import classify as C  # noqa: E402  审定默认：convertible + optional-custom=keep + temperature 0 + 3 次投票

log = logging.getLogger("selection")
WID = f"sel-{socket.gethostname()}-{os.getpid()}"
POLL = 5
PROFILE = os.environ.get("EHUNT_PROFILE_DIR", os.path.expanduser("~/.local/share/etsy-selection/ehunt-profile"))
LOGDIR = os.environ.get("SEL_LOG_DIR", os.path.expanduser("~/.local/share/etsy-selection/logs"))
NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

class Store:
    """最小 PostgREST 客户端（service_role，只在 box 的 worker.env 中；绝不打印）。只写：run/batch 汇总数 + 符合要求的 sel_items。"""
    def __init__(self):
        self.u = os.environ["SUPABASE_URL"].rstrip("/"); self.k = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    def _req(self, method, path, body=None, prefer="return=representation"):
        r = urllib.request.Request(self.u + path, None if body is None else json.dumps(body, default=str).encode(), method=method,
                                   headers={"apikey": self.k, "Authorization": "Bearer " + self.k, "Content-Type": "application/json", "Prefer": prefer})
        with urllib.request.urlopen(r, timeout=60) as f:
            t = f.read(); return json.loads(t) if t else None
    def claim(self): r = self._req("POST", "/rest/v1/rpc/sel_claim_run", {"p_worker": WID}); return r if r and r.get("id") else None
    def run(self, rid): return (self._req("GET", f"/rest/v1/sel_runs?id=eq.{rid}&select=*") or [{}])[0]
    def patch_run(self, rid, **kw): self._req("PATCH", f"/rest/v1/sel_runs?id=eq.{rid}", kw, "return=minimal")
    def open_batch(self, rid):
        r = self._req("GET", f"/rest/v1/sel_batches?run_id=eq.{rid}&status=in.(queued,running)&order=batch_no&limit=1"); return r[0] if r else None
    def patch_batch(self, bid, **kw): self._req("PATCH", f"/rest/v1/sel_batches?id=eq.{bid}", kw, "return=minimal")
    def new_batch(self, body): self._req("POST", "/rest/v1/sel_batches", body, "return=minimal")
    def passed_ids(self, rid): return {str(x["etsy_listing_id"]) for x in (self._req("GET", f"/rest/v1/sel_items?run_id=eq.{rid}&select=etsy_listing_id") or [])}
    def insert_item(self, body): self._req("POST", "/rest/v1/sel_items?on_conflict=run_id,etsy_listing_id", body, "return=minimal,resolution=ignore-duplicates")
    def purge(self): return self._req("POST", "/rest/v1/rpc/sel_purge_older_than", {})

def scrape_ehunt(keyword, params, start_rank, end_rank):
    """读 EHunt 排名 start_rank..end_rank（含）：界面设好筛选 → 跳到对应页（每页 20）→ 逐行读 DOM。只在内存里产出行，不落盘。"""
    import re
    from playwright.sync_api import sync_playwright
    U = ui(); ps = int(U["page_size"]); num = lambda s: float("".join(ch for ch in (s or "") if ch.isdigit() or ch == ".") or 0)
    def d7_of(t): m = re.search(U["d7_regex"], t or ""); return int(m.group(1).replace(",", "")) if m else 0
    def total_of(t):  # “637” / “6.26K” / “1.2M”
        t = re.split(r"↑", t or "")[0].strip().upper(); k = 1000 if t.endswith("K") else 1000000 if t.endswith("M") else 1
        return int(round(num(t) * k))
    page = (start_rank - 1) // ps + 1
    with sync_playwright() as p:
        ctx, pg = _open_list(p, keyword, params)
        try:
            def first_href():
                e = pg.query_selector(U["row"] + " " + U["title"]); return e.get_attribute("href") if e else None
            def wait_page(n, old):  # SPA 翻页：等到当前页码 = n 且首行变化
                for _ in range(60):
                    a_ = pg.query_selector(U["active_page"])
                    if a_ and a_.inner_text().strip() == str(n) and first_href() != old: break
                    time.sleep(0.5)
                else: raise RuntimeError(f"翻页到第 {n} 页超时")
                time.sleep(1.5)
            pg.wait_for_selector(U["row"], timeout=30000)
            if page > 1:  # 续读：分页 “Go to” 输入框
                old = first_href(); pg.fill(U["jump_input"], str(page)); pg.keyboard.press("Enter"); wait_page(page, old)
            rank = (page - 1) * ps
            while rank < end_rank:
                pg.wait_for_selector(U["row"], timeout=30000); t_page = time.time()
                log.info("ehunt page %s: reading rows (rank %s→, target %s)", page, rank + 1, end_rank)
                for tr in pg.query_selector_all(U["row"]):
                    a = tr.query_selector(U["title"])
                    if not a: continue
                    rank += 1
                    if rank < start_rank: continue
                    if rank > end_rank: break
                    lk = tr.query_selector(U["link"]); href = (lk.get_attribute("href") if lk else "") or a.get_attribute("href") or ""
                    lid = "".join(ch for ch in href.rstrip("/").split("/listing/")[-1].split("/product-detail/")[-1].split("/")[0] if ch.isdigit())
                    img = tr.query_selector(U["img"]); cell = lambda k: (lambda e: e.inner_text() if e else "")(tr.query_selector(U[k]))
                    yield {"rank": rank, "page": page, "title": a.inner_text().strip(), "listing_id": lid, "url": f"https://www.etsy.com/listing/{lid}", "image_url": img.get_attribute("src") if img else "",
                           "price": num(cell("price")), "total_sales": total_of(cell("total")), "sales_7d": d7_of(cell("d7"))}
                    time.sleep(0.3)
                log.info("ehunt page %s done: rank=%s, %.0fs", page, rank, time.time() - t_page)
                nxt = pg.query_selector(U["next"])
                if not nxt or rank >= end_rank: break
                old = first_href(); nxt.click(); page += 1; wait_page(page, old)
        finally: ctx.close()

# 标准筛选（需求文档 §2）：数量与抓取都必须带上，否则会拿到未筛选的总数（如 43,805）。
# EHunt 的筛选不在 URL 里（https://ehunt.ai/etsy-product-research 无查询参数），只在页面状态里 → 必须通过界面操作设置。
FILTER_DEFAULTS = {"price_min": 8, "price_max": 40, "min_sales": 100, "min_sales_7d": 4, "sort": "↓7-day sales"}  # 7 天销量 >3 即 Min 4
# 界面选择器/文字（2026-10-16 已登录实测校准；EHunt 改版时用 EHUNT_UI（JSON）覆盖，不改代码）
UI_DEFAULTS = {
    "url": "https://ehunt.ai/etsy-product-research",
    "banner_close": "button.ehunt-ai-banner__close",          # 顶部 “Meet the new EHunt AI” 横幅会挡住点击
    "search_input": "input[placeholder^='Enter a product name, tag, product ID']",
    "search_btn": ".el-input-group__append >> text=Search",   # 不能用 button:has-text('Search')：会命中 “Batch search” 弹窗
    "price_dd": "span.truncate:text-is('Price(USD)')",
    "sales_dd": "span.truncate:text-is('Sales')",
    "popover": ".el-popover[aria-hidden='false']",             # 当前展开的下拉面板
    "total_sales_line": "xpath=.//div[normalize-space(text())='Total sales:']/..",
    "sales7_line": "xpath=.//div[normalize-space(text())='7-day sales:']/..",
    "chip_price": "Price: {price_min}~{price_max}", "chip_total": "Total sales: {min_sales}~", "chip_7d": "7-day sales: {min_sales_7d}~",  # 10-16 实测确认；Min 为闭区间
    "min_input": "input[placeholder='Min']", "max_input": "input[placeholder='Max']",
    "ok_btn": "[role=button]:text-is('OK')",                   # div[role=button]，不是 <button>；只在 popover 内找
    "sort_dd": ":text('Export to CSV') >> xpath=following::*[contains(normalize-space(.),'sales')][1]",
    "selected_row": ":text('Selected:') >> xpath=..",
    "count_text": ":text-matches('Find [0-9,]+ products')",
    "count_regex": r"Find ([\d,]+) products",
    # 列表（Element Plus el-table）：第 1 列 商品信息（图/标题/价格），第 2 列 Total sales + 绿色 ↑N（7 天销量）
    "row": "table.el-table__body tr.el-table__row", "title": "a.product-title-clamp", "link": "a[href*='etsy.com/listing/']",
    "price": "td:nth-child(1) span.text-brand", "total": "td:nth-child(2) span:first-child",
    "d7": "td:nth-child(2)", "d7_regex": r"↑\s*([\d,]+)", "img": "td:nth-child(1) img",
    "next": "button.btn-next:not([disabled])", "active_page": ".el-pager li.is-active",
    "page_size": 20, "jump_input": ".el-pagination__jump input",   # “Go to”/跳至 输入框，回车跳页
}
def ui(): return {**UI_DEFAULTS, **json.loads(os.environ.get("EHUNT_UI", "{}"))}
def filters(params): return {**FILTER_DEFAULTS, **json.loads(os.environ.get("EHUNT_FILTERS", "{}")), **{k: v for k, v in (params or {}).items() if k in FILTER_DEFAULTS}}

def _open_list(p, keyword, params):
    """打开 EHunt 产品研究页并通过界面设置 关键词 + 价格 + 总销量 + 排序，校验 Selected: 标签后返回。"""
    U, F = ui(), filters(params)
    ctx = p.chromium.launch_persistent_context(PROFILE, headless=True,
                                               viewport={"width": 1440, "height": 1000}, locale="en-US")
    pg = ctx.pages[0] if ctx.pages else ctx.new_page(); pg.goto(U["url"], wait_until="networkidle", timeout=60000)
    if "login" in pg.url: ctx.close(); raise RuntimeError("EHunt 登录已失效：需要在 box 上运行 --login 重新人工登录")
    try:
        b = pg.query_selector(U["banner_close"])
        if b and b.is_visible(): b.click(); time.sleep(0.5)
        pg.fill(U["search_input"], keyword); pg.click(U["search_btn"]); pg.wait_for_load_state("networkidle"); time.sleep(2)
        # Basic: Price(USD) → 自定义 Min/Max → OK（都在展开的 popover 里）
        pg.click(U["price_dd"]); time.sleep(1); pop = pg.locator(U["popover"])
        pop.locator(U["min_input"]).first.fill(str(F["price_min"])); pop.locator(U["max_input"]).first.fill(str(F["price_max"]))
        pop.locator(U["ok_btn"]).first.click(); pg.wait_for_load_state("networkidle"); time.sleep(2)
        # Sales → Total sales: Min 100 + 7-day sales: Min 4（>3）→ OK
        pg.click(U["sales_dd"]); time.sleep(1); pop = pg.locator(U["popover"])
        pop.locator(U["total_sales_line"]).locator(U["min_input"]).first.fill(str(F["min_sales"]))
        pop.locator(U["sales7_line"]).locator(U["min_input"]).first.fill(str(F["min_sales_7d"]))
        pop.locator(U["ok_btn"]).first.click(); pg.wait_for_load_state("networkidle"); time.sleep(2)
        # 排序默认 ↓7-day sales，校验
        st = pg.locator(U["sort_dd"]).first.inner_text().strip()
        if "7-day" not in st: raise RuntimeError(f"排序不是 7 天销量降序：{st}")
        sel = pg.locator(U["selected_row"]).first.inner_text()
        need = [U["chip_price"].format(**F), U["chip_total"].format(**F), U["chip_7d"].format(**F)]
        miss = [x for x in need if x.replace(" ", "") not in sel.replace(" ", "")]
        if miss: raise RuntimeError(f"筛选未生效，Selected: 缺少 {miss}（实际：{sel[:120]}）")
    except Exception:
        try: os.makedirs(LOGDIR, exist_ok=True); pg.screenshot(path=f"{LOGDIR}/ehunt-fail-{int(time.time())}.png")
        except Exception: pass
        ctx.close(); raise
    return ctx, pg

def count_ehunt(keyword, params):
    """第 1 步：只读结果数文字 “Find 20 products”。"""
    import re
    from playwright.sync_api import sync_playwright
    U = ui()
    with sync_playwright() as p:
        ctx, pg = _open_list(p, keyword, params)
        try:
            txt = pg.locator(U["count_text"]).first.inner_text()
            m = re.search(U["count_regex"], txt)
            if not m: raise RuntimeError(f"未识别结果数文字：{txt[:80]}")
            return int(m.group(1).replace(",", ""))
        finally: ctx.close()

def fake_scrape(path, start_rank=1, end_rank=10**9):
    for r in json.load(open(path)):
        if start_rank <= r["rank"] <= end_rank: time.sleep(0.05); yield {**r, "page": (r["rank"] - 1) // 20 + 1}

# 排除原因：不入库，只写 box 本地 JSONL（3 天后自动删除），供 序猿 用 lookup.py 抽查
EXCL_DIR = os.environ.get("SEL_EXCL_DIR", os.path.expanduser("~/.local/share/etsy-selection/exclusion-logs"))
KEEP_SECONDS = 3 * 86400
def log_exclusion(run, batch_no, row, reason, detail, source):
    os.makedirs(EXCL_DIR, mode=0o700, exist_ok=True)
    with open(os.path.join(EXCL_DIR, f"{run['id']}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": NOW(), "run_id": run["id"], "keyword": run.get("keyword"), "batch_no": batch_no, "rank": row.get("rank"),
                            "listing_id": str(row["listing_id"]), "title": row.get("title"), "reason": reason, "detail": detail, "source": source}, ensure_ascii=False) + "\n")
def purge_local():
    if not os.path.isdir(EXCL_DIR): return 0
    n = 0; cutoff = time.time() - KEEP_SECONDS
    for fn in os.listdir(EXCL_DIR):
        fp = os.path.join(EXCL_DIR, fn)
        if fn.endswith(".jsonl") and os.path.getmtime(fp) < cutoff: os.remove(fp); n += 1
    return n

SEEN = {}  # run_id → 本进程已见过的 listing（含被排除的，只在内存）；跨批去重
def process_batch(store, run, batch, scraper):
    rid, bno = run["id"], batch["batch_no"]
    seen = SEEN.setdefault(rid, set()) | store.passed_ids(rid); SEEN[rid] = seen
    cnt = {"scraped": 0, "passed": 0, "excluded": 0, "dup": 0}; reasons = {}; calls = 0
    store.patch_batch(batch["id"], status="running", started_at=NOW())
    def flush(final=False):
        store.patch_batch(batch["id"], scraped_count=cnt["scraped"], passed_count=cnt["passed"], excluded_count=cnt["excluded"], dup_count=cnt["dup"], excluded_by_reason=reasons)
        store.patch_run(rid, heartbeat_at=NOW())
    for row in scraper:
        if cnt["scraped"] % 20 == 0 and store.run(rid).get("status") == "canceled":
            store.patch_batch(batch["id"], status="canceled", finished_at=NOW()); flush(); return "canceled"
        cnt["scraped"] += 1; lid = str(row["listing_id"])
        if lid in seen: cnt["dup"] += 1; continue          # 跨批/同批重复：不重复调用、不重复入库
        seen.add(lid)
        o = C.dedupe([row])[0]
        r = C.guard(o) or C.rule(o["title"])                 # ① 标题规则 + 7 天销量兜底：命中即排除，不调豆包
        if r:
            g = C.reason_group(r[1]); reasons[g] = reasons.get(g, 0) + 1; cnt["excluded"] += 1
            log_exclusion(run, bno, row, r[1], r[2], "兜底" if r[1] == "7天销量≤3" else "规则")
        else:                                                 # ② 规则通过 → 豆包视觉×3
            try: o, usage, n = C.classify_one(o); calls += n
            except Exception as e: o = {**o, "结果": "待确认", "备注": f"视觉失败：{str(e)[:80]}", "原因": "视觉调用失败"}   # 拿不准就保留
            if o["结果"] == "排除":
                g = C.reason_group(o.get("原因", "")); reasons[g] = reasons.get(g, 0) + 1; cnt["excluded"] += 1
                log_exclusion(run, bno, row, o.get("原因"), o.get("说明"), "视觉")
            else:                                             # ③ 只有符合要求的入库
                sp = lambda v: [x.strip() for x in (v or "").split(";") if x.strip()]
                store.insert_item({"run_id": rid, "batch_id": batch["id"], "user_id": run["user_id"], "batch_no": bno, "etsy_listing_id": int(lid),
                    "rank": row.get("rank"), "page": row.get("page"), "title": o["title"], "price_min": o["price_min"], "price_max": o["price_max"],
                    "total_sales": o["total_sales"], "sales_7d": o["sales_7d"], "image_url": o.get("image_url_hd") or o.get("image_url"),
                    "verdict": o["结果"], "reason": o.get("原因") or None, "note": o.get("备注") or None, "explain": o.get("说明"), "material": o.get("材质"),
                    "acrylic_fit": o.get("acrylic_fit") or None, "custom_type": o.get("定制类型"), "selling_points": sp(o.get("核心卖点")),
                    "occasions": sp(o.get("适用场合")), "votes": o.get("votes")})
                cnt["passed"] += 1
        if cnt["scraped"] % 5 == 0:   # 每 5 条刷新一次进度（网页进度条 = 本批 scraped_count / 本批条数）
            store.patch_batch(batch["id"], last_page=row.get("page")); flush()
        if cnt["scraped"] % 20 == 0: log.info("run %s batch %s: scraped=%s passed=%s excluded=%s dup=%s vision_calls=%s", rid, bno, cnt["scraped"], cnt["passed"], cnt["excluded"], cnt["dup"], calls)
    # 本批完成：只在这里更新“还剩 N 条未读”
    run = store.run(rid); remaining = max(0, (run.get("max_rows") or 0) - batch["offset_end"])
    store.patch_batch(batch["id"], status="done", finished_at=NOW(), remaining_after=remaining); flush()
    agg = {k: (run.get(f"{k}_count") or 0) + cnt[k] for k in ("scraped", "passed", "excluded")}
    rr = dict(run.get("excluded_by_reason") or {}); [rr.__setitem__(k, rr.get(k, 0) + v) for k, v in reasons.items()]
    upd = dict(scraped_count=agg["scraped"], passed_count=agg["passed"], excluded_count=agg["excluded"], excluded_by_reason=rr,
               vision_calls=(run.get("vision_calls") or 0) + calls, remaining_count=remaining)
    if remaining <= 0 or bno >= 5:
        store.patch_run(rid, status="succeeded", finished_at=NOW(), **upd)
    elif run.get("read_all"):  # 一键读取全部剩余：自动排下一批
        s0 = batch["offset_end"] + 1; e0 = min(batch["offset_end"] + (run.get("batch_size") or 1000), run["max_rows"])
        store.new_batch({"run_id": rid, "user_id": run["user_id"], "batch_no": bno + 1, "offset_start": s0, "offset_end": e0, "page_start": (s0 + 19) // 20, "page_end": (e0 + 19) // 20})
        store.patch_run(rid, status="queued", current_batch=bno + 1, **upd)
    else:
        store.patch_run(rid, status="batch_done", **upd)
    return "done"

def login(url):
    """一次性人工登录：有头浏览器开在 box 显示器上，等人登录完成后关闭窗口；不自动填写任何账号信息。"""
    from playwright.sync_api import sync_playwright
    os.environ.setdefault("DISPLAY", ":1"); os.makedirs(PROFILE, mode=0o700, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=False, viewport={"width": 1440, "height": 900}, locale="en-US")
        pg = ctx.pages[0] if ctx.pages else ctx.new_page(); pg.goto(url)
        print(f"已在 DISPLAY={os.environ['DISPLAY']} 打开 {url}；请人工登录 EHunt，完成后关闭浏览器窗口（登录状态保存在 {PROFILE}）", flush=True)
        try: pg.wait_for_event("close", timeout=0)
        except Exception: pass
        try: ctx.close()
        except Exception: pass
    print("登录窗口已关闭，profile 已保存", flush=True)

def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--once", action="store_true"); ap.add_argument("--fake", help="回放 JSON，代替真实抓取")
    ap.add_argument("--login", action="store_true", help="一次性人工登录 EHunt（有头浏览器，box 显示器）")
    ap.add_argument("--login-url", default="https://ehunt.ai/etsy-product-research")
    a = ap.parse_args(argv); logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.login: return login(a.login_url)
    store = Store(); last_purge = 0
    while True:
        if time.time() - last_purge > 86400:  # 启动时 + 每 24 小时：DB 3 天清理（pg_cron 不可用时的兜底）+ 本地排除日志清理
            try: log.info("purge db=%s local=%s", store.purge(), purge_local())
            except Exception: log.warning("purge failed", exc_info=True)
            last_purge = time.time()
        run = store.claim()
        if run:
            log.info("claimed run %s kw=%s status=%s", run["id"], run["keyword"], run["status"])
            if run["status"] == "counting":
                try:
                    n = len(json.load(open(a.fake))) if a.fake else count_ehunt(run["keyword"], run.get("params") or {})
                    store.patch_run(run["id"], status="counted", total_count=n, counted_at=NOW(), heartbeat_at=NOW())
                except Exception as e:
                    log.exception("count failed"); store.patch_run(run["id"], status="failed", error=str(e)[:300], finished_at=NOW())
            else:
                b = store.open_batch(run["id"])
                if b:
                    try:
                        sc = fake_scrape(a.fake, b["offset_start"], b["offset_end"]) if a.fake else scrape_ehunt(run["keyword"], run.get("params") or {}, b["offset_start"], b["offset_end"])
                        process_batch(store, run, b, sc)
                    except Exception as e:
                        log.exception("batch failed"); store.patch_batch(b["id"], status="failed", finished_at=NOW())
                        store.patch_run(run["id"], status="failed", error=str(e)[:300], finished_at=NOW())
                else: store.patch_run(run["id"], status="failed", error="没有待读批次", finished_at=NOW())
        if a.once: break
        time.sleep(POLL)

if __name__ == "__main__": main()
