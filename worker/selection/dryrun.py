"""本地试跑（不连数据库）：真实 EHunt 计数 + 抓取 + 规则/豆包分类，结果只写本地 JSON，绝不写 Supabase。
用法（在 worker/ 下）：python -m selection.dryrun "acrylic decoration" --max 20 [--no-vision] [--count-only] [--out /tmp/sel-dryrun]
需要已登录的 EHUNT_PROFILE_DIR；豆包需要 ARK_API_KEY（--no-vision 时只跑规则，规则通过的记为“待视觉”）。"""
import argparse, json, os, sys, time
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("keyword"); ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--no-vision", action="store_true"); ap.add_argument("--count-only", action="store_true")
    ap.add_argument("--out", default=os.path.expanduser("~/.local/share/etsy-selection/dryrun"))
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    os.environ["SEL_EXCL_DIR"] = a.out  # 排除日志也只写本地试跑目录
    from selection import ehunt_worker as W
    W.EXCL_DIR = a.out
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"): os.environ.pop(k, None)  # 保险：本进程拿不到数据库凭据
    t0 = time.time(); n = W.count_ehunt(a.keyword, {}); print(f"count[{a.keyword}] = {n}", flush=True)
    if a.count_only: return
    time.sleep(3)
    items, run = [], {"id": f"dryrun-{int(time.time())}", "keyword": a.keyword, "user_id": "dryrun", "max_rows": min(n, a.max), "status": "running"}
    class Stub:  # 与 Store 相同接口，全部在内存
        def passed_ids(self, rid): return set()
        def patch_batch(self, bid, **kw): pass
        def patch_run(self, rid, **kw): run.update(kw)
        def run(self, rid): return dict(run)
        def new_batch(self, body): pass
        def insert_item(self, body): items.append(body)
    if a.no_vision:
        W.classify_parallel = lambda o: ({**o, "结果": "待视觉", "原因": "", "备注": "dry-run 未调用豆包"}, {}, 0)
    rows = []
    def tap(g):
        for r in g: rows.append(r); yield r
    t1 = time.time(); batch = {"id": "b1", "batch_no": 1, "offset_start": 1, "offset_end": run["max_rows"]}
    W.process_batch(Stub(), run, batch, tap(W.scrape_ehunt(a.keyword, {}, 1, run["max_rows"])))
    out = os.path.join(a.out, f"{run['id']}.result.json")
    json.dump({"keyword": a.keyword, "count": n, "rows": rows, "passed_items": items, "run": run}, open(out, "w"), ensure_ascii=False, indent=1, default=str)
    print(json.dumps({k: run.get(k) for k in ("scraped_count", "passed_count", "excluded_count", "excluded_by_reason", "vision_calls")}, ensure_ascii=False))
    print(f"elapsed total={time.time()-t0:.0f}s scrape+classify={time.time()-t1:.0f}s vision_concurrency={W.VISION_CONC} ark_inflight={W.ARK_INFLIGHT}")
    print("result:", out, "| exclusions:", os.path.join(a.out, run["id"] + ".jsonl"))
if __name__ == "__main__": main()
