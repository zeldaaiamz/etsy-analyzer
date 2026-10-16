#!/usr/bin/env python3
"""获取链接查询（网页无搜索，由 序猿 在对话里用本脚本查）。只读。
  lookup.py runs [--keyword kw]                 最近 3 天的任务与批次汇总（DB）
  lookup.py links <run_id> [--batch N]          符合要求的链接（DB）
  lookup.py listing <listing_id>                某 listing 是否出现在符合要求结果里（DB），以及本地排除日志里的原因
  lookup.py excluded <run_id> [--reason 定制]   本地排除日志（box ~/.local/share/etsy-selection/exclusion-logs，3 天后删除）
环境：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY（从 ~/.config/etsy-selection/worker.env 读取，不打印）"""
import argparse, json, os, urllib.request, urllib.parse, glob
EXCL_DIR = os.environ.get("SEL_EXCL_DIR", os.path.expanduser("~/.local/share/etsy-selection/exclusion-logs"))
def env():
    f = os.environ.get("SEL_ENV_FILE", os.path.expanduser("~/.config/etsy-selection/worker.env"))
    if os.path.exists(f):
        for l in open(f):
            if "=" in l and not l.lstrip().startswith("#"): k, v = l.strip().split("=", 1); os.environ.setdefault(k, v.strip('"\''))
def get(path):
    u, k = os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    with urllib.request.urlopen(urllib.request.Request(u + "/rest/v1/" + path, headers={"apikey": k, "Authorization": "Bearer " + k}), timeout=60) as r: return json.load(r)
def excl(run=None, listing=None, reason=None):
    for fp in sorted(glob.glob(os.path.join(EXCL_DIR, f"{run or '*'}.jsonl"))):
        for l in open(fp, encoding="utf-8"):
            d = json.loads(l)
            if (listing and d["listing_id"] != str(listing)) or (reason and reason not in (d.get("reason") or "")): continue
            yield d
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("runs"); a.add_argument("--keyword")
    a = sp.add_parser("links"); a.add_argument("run_id"); a.add_argument("--batch", type=int)
    a = sp.add_parser("listing"); a.add_argument("listing_id")
    a = sp.add_parser("excluded"); a.add_argument("run_id"); a.add_argument("--reason")
    A = ap.parse_args(); env()
    if A.cmd == "runs":
        q = "sel_runs?select=id,keyword,status,total_count,max_rows,scraped_count,passed_count,excluded_count,excluded_by_reason,remaining_count,created_at,sel_batches(batch_no,offset_start,offset_end,status,passed_count,finished_at)&order=created_at.desc&limit=50"
        if A.keyword: q += "&keyword=ilike." + urllib.parse.quote(f"*{A.keyword}*")
        print(json.dumps(get(q), ensure_ascii=False, indent=1))
    elif A.cmd == "links":
        q = f"sel_items?run_id=eq.{A.run_id}&select=etsy_url&order=rank" + (f"&batch_no=eq.{A.batch}" if A.batch else "")
        print("\n".join(x["etsy_url"] for x in get(q)))
    elif A.cmd == "listing":
        print(json.dumps({"符合要求（DB，最近 3 天）": get(f"sel_items?etsy_listing_id=eq.{int(A.listing_id)}&select=run_id,batch_no,verdict,title,price_min,sales_7d,custom_type,acrylic_fit,created_at"),
                          "排除记录（box 本地日志，最近 3 天）": list(excl(listing=A.listing_id))}, ensure_ascii=False, indent=1))
    else:
        for d in excl(A.run_id, reason=A.reason): print(json.dumps(d, ensure_ascii=False))
if __name__ == "__main__": main()
