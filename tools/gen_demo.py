"""用 worker 的 mock 管线生成前端演示数据 web/js/demo.js（纯示例，不含任何密钥/真实数据）。"""
import base64, json, sys, datetime as dt
sys.path.insert(0, "worker")
from analyzer.config import Config
from analyzer.providers import build_providers
from analyzer.providers.mock import MockScreenshot
from analyzer.queue import MemoryStore
from analyzer.pipeline import process_job

URLS = ["https://www.etsy.com/listing/4363176935/monster-boo-basket-tag-halloween-basket",
        "https://www.etsy.com/listing/1580023428/personalized-teacher-gift-tote-bag",
        "https://www.etsy.com/listing/1580023401/minimalist-line-art-wall-print"]
COLORS = ["#f97316", "#8b5cf6", "#0ea5e9", "#10b981"]

def thumb(text, color):
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           f'<stop offset="0" stop-color="{color}"/><stop offset="1" stop-color="#1e293b"/></linearGradient></defs>'
           f'<rect width="300" height="300" fill="url(#g)"/><text x="150" y="165" font-size="34" text-anchor="middle" fill="#fff" font-family="sans-serif">{text}</text></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()

cfg, store = Config(), MemoryStore()
etsy, amazon, llm = build_providers(cfg)
for u in URLS:
    store.enqueue(u, user_id="demo-user")
while (job := store.claim_next_job("demo")):
    process_job(job, store, etsy, amazon, llm, 10, MockScreenshot())
now = dt.datetime(2026, 10, 14, 6, 0, tzinfo=dt.timezone.utc)
jobs = []
for i, j in enumerate(store.jobs.values()):
    jobs.append({k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in j.items()} | {"created_at": (now - dt.timedelta(minutes=10 * i)).isoformat()})
jobs.append({"id": "demo-running", "etsy_url": "https://www.etsy.com/listing/1700000001/custom-pet-portrait-mug", "status": "running", "stage": "search_amazon",
             "etsy_listing_id": 1700000001, "created_at": (now + dt.timedelta(minutes=2)).isoformat()})
jobs.append({"id": "demo-failed", "etsy_url": "https://www.etsy.com/listing/1700000002/x", "status": "failed", "etsy_listing_id": 1700000002,
             "error": "Etsy API 返回 404：商品已下架", "created_at": (now - dt.timedelta(hours=2)).isoformat()})
etsy_rows = []
for i, (jid, p) in enumerate(store.etsy.items()):
    etsy_rows.append({"job_id": jid, "title": p["title"], "price": p["price"], "listing_id": p["listing_id"],
                      "image_url": thumb(p["title"].split()[0], COLORS[i % 4])})
etsy_rows.append({"job_id": "demo-running", "title": "Custom Pet Portrait Mug - Handmade", "image_url": thumb("Pet", COLORS[3])})
analyses = [dict(v, job_id=k) for k, v in store.analyses.items()]
signed = {path: f"data:{ct};base64," + base64.b64encode(data).decode() for path, (data, ct) in store.files.items()}
fx = {"jobs": jobs, "analyses": analyses, "etsy": etsy_rows, "snaps": [dict(s, id=i) for i, s in enumerate(store.snapshots)], "signed": signed}
open("web/js/demo.js", "w").write("// 自动生成（tools/gen_demo.py）：?demo=1 时使用的纯前端示例数据\nexport const fixtures = () => (" + json.dumps(fx, ensure_ascii=False, default=str) + ");\n")
print("ok", len(jobs), "jobs", len(fx["snaps"]), "snaps")
