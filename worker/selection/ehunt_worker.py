"""选品筛选 worker（按需）：认领 sel_runs → Playwright 已登录 EHunt 的浏览器逐条读列表页 → 每条立即分类 → 写 sel_items。
读页面 DOM，不调用 EHunt API。部署设计见 deploy/etsy-selection-worker.service 与 tools/etsy_selection/README.md（未部署）。

运行：python -m selection.ehunt_worker            # 常驻轮询
      python -m selection.ehunt_worker --fake list.json --once   # 不开浏览器，用回放 JSON 模拟抓取（测试）
环境：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY、ARK_API_KEY、EHUNT_PROFILE_DIR（持久化登录目录）、EHUNT_LIST_URL（含 {kw} 的列表页 URL 模板）
"""
import argparse, json, os, socket, sys, time, logging, urllib.request, urllib.parse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "etsy_selection"))
import classify as C  # noqa: E402  审定默认：convertible + optional-custom=keep + temperature 0 + 3 次投票

log = logging.getLogger("selection")
WID = f"sel-{socket.gethostname()}-{os.getpid()}"
POLL = 5
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
    """逐条产出 {rank,title,price,total_sales,sales_7d,listing_id,image_url}。读已登录的 EHunt 列表页 DOM。
    选择器集中在 SEL，上线前需对照真实页面校准（EHunt 改版会失效）。"""
    from playwright.sync_api import sync_playwright
    SEL = json.loads(os.environ.get("EHUNT_SELECTORS", "{}")) or {
        "row": "table tbody tr", "title": "a[href*='etsy.com/listing']", "price": "td.price", "total": "td.total-sales", "d7": "td.sales-7d",
        "img": "img", "next": "button.btn-next:not([disabled])"}
    url = os.environ["EHUNT_LIST_URL"].format(kw=urllib.parse.quote(keyword), **{k: urllib.parse.quote(str(v)) for k, v in params.items()})
    num = lambda s: float("".join(ch for ch in (s or "") if ch.isdigit() or ch == ".") or 0)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(os.environ.get("EHUNT_PROFILE_DIR", "/opt/etsy-analyzer/ehunt-profile"), headless=True,
                                                   viewport={"width": 1440, "height": 1000}, locale="zh-CN")
        pg = ctx.pages[0] if ctx.pages else ctx.new_page(); pg.goto(url, wait_until="networkidle", timeout=60000)
        if "login" in pg.url: raise RuntimeError("EHunt 登录已失效，需要在服务器上重新登录持久化目录")
        rank = 0
        while rank < limit:
            pg.wait_for_selector(SEL["row"], timeout=30000)
            for tr in pg.query_selector_all(SEL["row"]):
                a = tr.query_selector(SEL["title"]); 
                if not a: continue
                href = a.get_attribute("href") or ""; lid = "".join(ch for ch in href.split("/listing/")[-1].split("/")[0] if ch.isdigit())
                img = tr.query_selector(SEL["img"]); rank += 1
                yield {"rank": rank, "title": a.inner_text().strip(), "listing_id": lid, "image_url": img.get_attribute("src") if img else "",
                       "price": num((tr.query_selector(SEL["price"]) or a).inner_text()), "total_sales": int(num((tr.query_selector(SEL["total"]) or a).inner_text())),
                       "sales_7d": int(num((tr.query_selector(SEL["d7"]) or a).inner_text()))}
                if rank >= limit: break
                time.sleep(0.3)  # 读页面节流
            nxt = pg.query_selector(SEL["next"])
            if not nxt or rank >= limit: break
            nxt.click(); pg.wait_for_load_state("networkidle"); time.sleep(2)
        ctx.close()

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

def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--once", action="store_true"); ap.add_argument("--fake", help="回放 JSON，代替真实抓取")
    a = ap.parse_args(argv); logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store()
    while True:
        run = store.claim()
        if run:
            log.info("claimed run %s kw=%s", run["id"], run["keyword"])
            try: process(store, run, fake_scrape(a.fake) if a.fake else scrape_ehunt(run["keyword"], run.get("params") or {}, run.get("target_count") or 100))
            except Exception as e:
                log.exception("run failed"); store.patch_run(run["id"], status="failed", error=str(e)[:300], finished_at=NOW())
        if a.once: break
        time.sleep(POLL)

if __name__ == "__main__": main()
