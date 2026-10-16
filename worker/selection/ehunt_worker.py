"""选品筛选 worker（按需）：认领 sel_runs → Playwright 已登录 EHunt 的浏览器逐条读列表页 → 每条立即分类 → 写 sel_items。
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
    """最小 PostgREST 客户端（service_role，只在服务器 .env 中；绝不打印）。"""
    def __init__(self):
        self.u = os.environ["SUPABASE_URL"].rstrip("/"); self.k = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    def _req(self, method, path, body=None, prefer="return=representation"):
        r = urllib.request.Request(self.u + path, None if body is None else json.dumps(body, default=str).encode(), method=method,
                                   headers={"apikey": self.k, "Authorization": "Bearer " + self.k, "Content-Type": "application/json", "Prefer": prefer})
        with urllib.request.urlopen(r, timeout=60) as f:
            t = f.read(); return json.loads(t) if t else None
    def claim(self): r = self._req("POST", "/rest/v1/rpc/sel_claim_run", {"p_worker": WID}); return r if r and r.get("id") else None
    def run(self, rid): return (self._req("GET", f"/rest/v1/sel_runs?id=eq.{rid}&select=status") or [{}])[0]
    def patch_run(self, rid, **kw): self._req("PATCH", f"/rest/v1/sel_runs?id=eq.{rid}", kw, "return=minimal")
    def raw(self, rid, row): self._req("POST", "/rest/v1/sel_raw_rows", {"run_id": rid, "rank": row.get("rank"), "etsy_listing_id": int(row["listing_id"]), "raw": row}, "return=minimal")
    def upsert_item(self, body):
        return self._req("POST", "/rest/v1/sel_items?on_conflict=run_id,etsy_listing_id", body, "return=representation,resolution=merge-duplicates")[0]
    def patch_item(self, iid, **kw): self._req("PATCH", f"/rest/v1/sel_items?id=eq.{iid}", kw, "return=minimal")

def scrape_ehunt(keyword, params, limit):
    """逐条产出 {rank,title,price,total_sales,sales_7d,listing_id,image_url}：界面设好筛选后读表格 DOM，每页 20 条翻页。"""
    from playwright.sync_api import sync_playwright
    U = ui(); num = lambda s: float("".join(ch for ch in (s or "") if ch.isdigit() or ch == ".") or 0)
    with sync_playwright() as p:
        ctx, pg = _open_list(p, keyword, params); rank = 0
        try:
            while rank < limit:
                pg.wait_for_selector(U["row"], timeout=30000)
                for tr in pg.query_selector_all(U["row"]):
                    a = tr.query_selector(U["title"])
                    if not a: continue
                    href = a.get_attribute("href") or ""; lid = "".join(ch for ch in href.split("/listing/")[-1].split("/")[0] if ch.isdigit())
                    img = tr.query_selector(U["img"]); cell = lambda k: (tr.query_selector(U[k]) or a).inner_text(); rank += 1
                    yield {"rank": rank, "title": a.inner_text().strip(), "listing_id": lid, "image_url": img.get_attribute("src") if img else "",
                           "price": num(cell("price")), "total_sales": int(num(cell("total"))), "sales_7d": int(num(cell("d7")))}
                    if rank >= limit: break
                    time.sleep(0.3)  # 读页面节流
                nxt = pg.query_selector(U["next"])
                if not nxt or rank >= limit: break
                nxt.click(); pg.wait_for_load_state("networkidle"); time.sleep(2)
        finally: ctx.close()

# 标准筛选（需求文档 §2）：数量与抓取都必须带上，否则会拿到未筛选的总数（如 43,805）。
# EHunt 的筛选不在 URL 里（https://ehunt.ai/etsy-product-research 无查询参数），只在页面状态里 → 必须通过界面操作设置。
FILTER_DEFAULTS = {"price_min": 8, "price_max": 40, "min_sales": 100, "sort": "↓7-day sales"}
# 界面选择器/文字（占位，按 2026-10-16 浏览器观察；EHunt 改版时用 EHUNT_UI（JSON）覆盖，不改代码）
UI_DEFAULTS = {
    "url": "https://ehunt.ai/etsy-product-research",
    "search_input": "input[placeholder^='Enter a product name, tag, product ID']",
    "search_btn": "button:has-text('Search')",
    "basic_row": ":text('Basic:')",
    "price_dd": "text=Price(USD)",
    "sales_dd": "text=Sales",
    "total_sales_line": ":text('Total sales:')",
    "min_input": "input[placeholder*='Min' i]", "max_input": "input[placeholder*='Max' i]",
    "ok_btn": "button:has-text('OK')",
    "sort_dd": ":text('Export to CSV') >> xpath=following::*[contains(normalize-space(.),'sales')][1]",
    "selected_row": ":text('Selected:') >> xpath=..",
    "count_text": ":text-matches('Find [\\d,]+ products')",
    "count_regex": r"Find ([\d,]+) products",
    "row": "table tbody tr", "title": "a[href*='etsy.com/listing']", "price": "td:nth-child(3)", "total": "td:nth-child(4)",
    "d7": "td:nth-child(5)", "img": "img", "next": "li.ant-pagination-next:not(.ant-pagination-disabled)",
    "page_size": 20,
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
        pg.fill(U["search_input"], keyword); pg.click(U["search_btn"]); pg.wait_for_load_state("networkidle"); time.sleep(1.5)
        # Basic: Price(USD) → 自定义 Min/Max → OK
        pg.click(U["price_dd"]); pg.fill(U["min_input"], str(F["price_min"])); pg.fill(U["max_input"], str(F["price_max"])); pg.click(U["ok_btn"]); time.sleep(1.5)
        # Sales → Total sales: Min → OK
        pg.click(U["sales_dd"]); line = pg.locator(U["total_sales_line"]).locator("xpath=..")
        line.locator(U["min_input"]).first.fill(str(F["min_sales"])); pg.click(U["ok_btn"]); pg.wait_for_load_state("networkidle"); time.sleep(1.5)
        # 排序默认 ↓7-day sales，校验
        st = pg.locator(U["sort_dd"]).first.inner_text().strip()
        if "7-day" not in st: raise RuntimeError(f"排序不是 7 天销量降序：{st}")
        sel = pg.locator(U["selected_row"]).first.inner_text()
        need = [f"Price: {F['price_min']}~{F['price_max']}", f"Total sales: {F['min_sales']}~"]
        miss = [x for x in need if x.replace(" ", "") not in sel.replace(" ", "")]
        if miss: raise RuntimeError(f"筛选未生效，Selected: 缺少 {miss}（实际：{sel[:120]}）")
    except Exception:
        try: os.makedirs(LOGDIR, exist_ok=True); pg.screenshot(path=f"{LOGDIR}/ehunt-fail-{int(time.time())}.png")
        except Exception: pass
        ctx.close(); raise
    return ctx, pg

def count_ehunt(keyword, params):
    """第 1 步：只读结果数文字 “Find 505 products, show you the first 505”。"""
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

def fake_scrape(path):
    for r in json.load(open(path)): time.sleep(0.2); yield r

def process(store, run, scraper):
    rid, seen = run["id"], {}
    n_scr = n_cls = 0; cost = 0; tokens = 0
    for row in scraper:
        if store.run(rid).get("status") == "canceled": log.info("run %s canceled", rid); return
        store.raw(rid, row); n_scr += 1
        lid = str(row["listing_id"]); g = seen.setdefault(lid, []); g.append(row)
        o = C.dedupe(g)[0]                       # 同 listing 合并（价格区间、销量取大、备注疑似变体）
        base = {"run_id": rid, "user_id": run["user_id"], "etsy_listing_id": int(lid), "ranks": o["ranks"], "title": o["title"], "price_min": o["price_min"],
                "price_max": o["price_max"], "total_sales": o["total_sales"], "sales_7d": o["sales_7d"], "dup_note": o["dup_note"],
                "image_url": o["image_url"].replace("il_120xN", "il_794xN"), "verdict": "待确认", "item_state": "scraped"}
        if len(g) > 1:  # 重复行：只更新合并字段，不重新分类
            store.upsert_item({k: v for k, v in base.items() if k not in ("verdict", "item_state")}); store.patch_run(rid, scraped_count=n_scr, heartbeat_at=NOW()); continue
        it = store.upsert_item(base); store.patch_run(rid, scraped_count=n_scr, heartbeat_at=NOW())
        store.patch_item(it["id"], item_state="classifying")
        try:
            o, usage, _ = C.classify_one(o)
            sp = lambda s: [x.strip() for x in (s or "").split(";") if x.strip()]
            store.patch_item(it["id"], verdict=o["结果"], reason=o.get("原因"), note=o.get("备注"), explain=o.get("说明"), source=o.get("来源"), material=o.get("材质"),
                             acrylic_fit=o.get("acrylic_fit") or None, custom_type=o.get("定制类型"), selling_points=sp(o.get("核心卖点")), occasions=sp(o.get("适用场合")),
                             votes=o.get("votes"), image_url=o.get("image_url_hd"), gate=o["gate"], gate_by="auto", item_state="classified", classified_at=NOW())
            tokens += usage.get("total_tokens", 0); cost += usage.get("prompt_tokens", 0) / 1e6 * C.PRICE_IN + usage.get("completion_tokens", 0) / 1e6 * C.PRICE_OUT
        except Exception as e:  # 单条失败不影响整批：保持待确认
            log.exception("classify failed"); store.patch_item(it["id"], item_state="error", gate="pending", note=f"分类失败：{str(e)[:120]}")
        n_cls += 1; store.patch_run(rid, classified_count=n_cls, heartbeat_at=NOW(), stats={"tokens": tokens, "cost_yuan": round(cost, 4)})
    store.patch_run(rid, status="succeeded", finished_at=NOW(), stats={"raw_rows": n_scr, "items": len(seen), "tokens": tokens, "cost_yuan": round(cost, 4)})

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
    store = Store()
    while True:
        run = store.claim()
        if run:
            log.info("claimed run %s kw=%s", run["id"], run["keyword"])
            if run["status"] == "counting":   # 轻量查询数量任务
                try:
                    n = len(json.load(open(a.fake))) if a.fake else count_ehunt(run["keyword"], run.get("params") or {})
                    store.patch_run(run["id"], status="counted", total_count=n, counted_at=NOW(), heartbeat_at=NOW())
                except Exception as e:
                    log.exception("count failed"); store.patch_run(run["id"], status="failed", error=str(e)[:300], finished_at=NOW())
                if a.once: break
                continue
            try: process(store, run, fake_scrape(a.fake) if a.fake else scrape_ehunt(run["keyword"], run.get("params") or {}, run.get("target_count") or 100))
            except Exception as e:
                log.exception("run failed"); store.patch_run(run["id"], status="failed", error=str(e)[:300], finished_at=NOW())
        if a.once: break
        time.sleep(POLL)

if __name__ == "__main__": main()
