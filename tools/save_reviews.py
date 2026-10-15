#!/usr/bin/env python3
"""把浏览器抓取的 Etsy 评论日期写入 Supabase（019：listing_reviews / listing_scrape_status）。
使用 worker/.env 中的 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY（不打印密钥）。

用法：
  python3 tools/save_reviews.py --list-library            # 列出产品库 listing_id 与 URL（去重）
  python3 tools/save_reviews.py reviews.json [--dry-run]   # 写入
  cat reviews.json | python3 tools/save_reviews.py -

reviews.json 格式（每个 listing 一项，两种写法均可）：
  {"1234567890": {"dates": ["2026-10-13", "2026-09-30"], "status": "ok"},
   "2222222222": {"status": "failed", "error": "captcha"},
   "3333333333": ["2026-10-01", "2026-08-15"]}          # 纯列表 = status ok
status=ok：替换该 listing 的公共 etsy_page 评论行（user_id 为空），并把 listing_scrape_status 置 ok；
status=failed：只更新 listing_scrape_status=failed（不动已有评论行）。attempts 每次 +1。
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path=os.path.join(ROOT, "worker", ".env")):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    url = os.environ.get("SUPABASE_URL") or env.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("缺少 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY（worker/.env）")
    return url.rstrip("/"), key


class Rest:
    def __init__(self, url, key):
        self.url, self.h = url, {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def __call__(self, method, path, params=None, body=None, prefer=None):
        u = f"{self.url}/rest/v1/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        h = dict(self.h, **({"Prefer": prefer} if prefer else {}))
        req = urllib.request.Request(u, method=method, headers=h,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                t = r.read()
                return json.loads(t) if t.strip() else None
        except urllib.error.HTTPError as e:   # 只输出响应体，不含请求头/密钥
            raise SystemExit(f"{method} {path} → HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")


def list_library(rest):
    rows, off = [], 0
    while True:
        page = rest("GET", "product_library", {"select": "etsy_listing_id,etsy_url,title", "order": "added_at.desc",
                                               "limit": 1000, "offset": off})
        rows += page
        if len(page) < 1000:
            break
        off += 1000
    seen = {}
    for r in rows:
        if r["etsy_listing_id"] and r["etsy_listing_id"] not in seen:
            seen[r["etsy_listing_id"]] = r
    for lid, r in seen.items():
        print(f"{lid}\t{r.get('etsy_url') or f'https://www.etsy.com/listing/{lid}'}")
    print(f"# {len(seen)} listings", file=sys.stderr)


def norm(item):
    if isinstance(item, list):
        item = {"dates": item, "status": "ok"}
    st = item.get("status", "ok")
    if st not in ("ok", "failed"):
        raise SystemExit(f"status 只能是 ok/failed：{st}")
    today = dt.date.today()
    dates = []
    for d in item.get("dates") or []:
        x = dt.date.fromisoformat(str(d)[:10])
        if not (dt.date(2005, 1, 1) <= x <= today):
            raise SystemExit(f"日期超出范围：{d}")
        dates.append(x)
    return st, sorted(dates, reverse=True), item.get("error")


def save(rest, data, dry):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for lid, item in data.items():
        lid = int(lid)
        st, dates, err = norm(item)
        prev = rest("GET", "listing_scrape_status", {"select": "attempts", "listing_id": f"eq.{lid}"}) or []
        attempts = (prev[0]["attempts"] if prev else 0) + 1
        print(f"{lid}: {st}, {len(dates)} dates" + (f" (newest {dates[0]})" if dates else "") + (" [dry-run]" if dry else ""))
        if dry:
            continue
        if st == "ok":
            rest("DELETE", "listing_reviews", {"listing_id": f"eq.{lid}", "source": "eq.etsy_page", "user_id": "is.null"},
                 prefer="return=minimal")
            cnt, rows = {}, []
            for d in dates:
                cnt[d] = cnt.get(d, 0) + 1
                rows.append({"listing_id": lid, "review_date": d.isoformat(), "source": "etsy_page",
                             "review_key": f"{d.isoformat()}#{cnt[d]}"})
            if rows:
                rest("POST", "listing_reviews", body=rows, prefer="return=minimal")
        rest("POST", "listing_scrape_status", {"on_conflict": "listing_id"},
             body={"listing_id": lid, "status": st, "error": None if st == "ok" else (err or "scrape failed")[:500],
                   "attempts": attempts, "updated_at": now},
             prefer="resolution=merge-duplicates,return=minimal")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="JSON 文件，- 表示 stdin")
    ap.add_argument("--list-library", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rest = Rest(*load_env())
    if a.list_library:
        return list_library(rest)
    if not a.file:
        ap.error("需要 JSON 文件或 --list-library")
    data = json.load(sys.stdin if a.file == "-" else open(a.file, encoding="utf-8"))
    save(rest, data, a.dry_run)


if __name__ == "__main__":
    main()
