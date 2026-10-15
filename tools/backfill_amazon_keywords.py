#!/usr/bin/env python3
"""PM-001 一次性回填：为已在产品库的产品生成「一键亚马逊搜索」关键词（只写 DB，不改线上 UI）。
数据来源：产品库快照 + 关联任务的 etsy_products（标题/标签/描述/评论）+ analyses（旧搜索词）。
DeepSeek 无法看图；目前流水线没有图片描述数据，所以不会产生 image 来源（脚本会统计）。
跳过人工编辑过的（keywords_edited_at 非空）和已有关键词的条目（--force 覆盖后者）。
用法：python3 tools/backfill_amazon_keywords.py [--env path/to/worker/.env] [--dry-run] [--force]
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "worker"))
from analyzer import amazon_keywords as akw  # noqa: E402
from analyzer.providers.llm_openai import OpenAICompatLLM, image_description  # noqa: E402
from analyzer.models import EtsyProduct  # noqa: E402

PROMPT = """你是亚马逊美国站选品专家。根据 Etsy 商品信息为「一键亚马逊搜索」生成搜索词。严格只输出 JSON：
{""" + akw.PROMPT_FIELD + """}
规则：
""" + akw.PROMPT_RULES + """
- old_keywords 是之前分析给出的搜索词，可参考（来源按其依据判断，一般是 title）。"""


def load_env(path):
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Rest:
    def __init__(self, url, key):
        self.base, self.h = url.rstrip("/") + "/rest/v1/", {"apikey": key, "Authorization": f"Bearer {key}",
                                                              "Content-Type": "application/json"}

    def req(self, method, path, body=None, prefer=None):
        h = dict(self.h, **({"Prefer": prefer} if prefer else {}))
        r = urllib.request.Request(self.base + path, method=method, headers=h,
                                   data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(r, timeout=60) as resp:
            t = resp.read()
            return json.loads(t) if t else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=os.path.join(ROOT, "worker", ".env"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--rebuild", action="store_true", help="不调用 LLM，只按当前规则重新校验已存的关键词")
    a = ap.parse_args()
    env = load_env(a.env)
    db = Rest(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    llm = OpenAICompatLLM(env.get("LLM_BASE_URL", "https://api.deepseek.com"), env["LLM_API_KEY"], env.get("LLM_MODEL", "deepseek-chat"))
    rows = db.req("GET", "product_library?select=id,job_id,etsy_listing_id,title,keywords,product_summary,stage,amazon_keywords,keywords_edited_at")
    report, img_used = [], 0
    for r in rows:
        if r.get("keywords_edited_at"):
            continue
        if a.rebuild:
            items = akw.build(r.get("amazon_keywords"), r.get("title") or "", fallback=r.get("keywords") or [])
            if items != r.get("amazon_keywords"):
                print(f"{r['etsy_listing_id']}: {[(k['keyword'], k['source']) for k in items]}")
                if not a.dry_run:
                    db.req("PATCH", f"product_library?id=eq.{r['id']}&keywords_edited_at=is.null", {"amazon_keywords": items}, "return=minimal")
                    if r.get("job_id"):
                        db.req("PATCH", f"analyses?job_id=eq.{urllib.parse.quote(r['job_id'])}", {"keyword_items": items}, "return=minimal")
            continue
        if r.get("amazon_keywords") and not a.force:
            continue
        ep, an = {}, {}
        if r.get("job_id"):
            q = urllib.parse.quote(r["job_id"])
            ep = (db.req("GET", f"etsy_products?job_id=eq.{q}&select=title,description,tags,materials,raw,reviews") or [{}])[0]
            an = (db.req("GET", f"analyses?job_id=eq.{q}&select=keywords,product_summary") or [{}])[0]
        title = ep.get("title") or r.get("title") or ""
        p = EtsyProduct(listing_id=r["etsy_listing_id"], title=title, description=ep.get("description") or "",
                        tags=ep.get("tags") or [], materials=ep.get("materials") or [], raw=ep.get("raw") or {})
        revs = [x.get("text", "")[:160] for x in (ep.get("reviews") or []) if isinstance(x, dict) and x.get("text")][:15]
        old = an.get("keywords") or r.get("keywords") or []
        info = {"title": title, "tags": p.tags[:13], "materials": p.materials[:8], "description": p.description[:1500],
                "product_summary": an.get("product_summary") or r.get("product_summary") or "",
                "old_keywords": old, "review_phrases": revs}
        img = image_description(p)
        if img:
            info["image_description"] = img
            img_used += 1
        j = {}
        for _ in range(2):  # 偶发非法 JSON：重试一次，仍失败则只用旧搜索词 + 原标题短语
            try:
                j = llm._chat(PROMPT, info)
                break
            except ValueError as e:
                print(f"    ! LLM JSON 解析失败，重试：{str(e)[:60]}")
        items = akw.build(j.get("amazon_keywords"), title, fallback=old)
        report.append({"id": r["id"], "listing_id": r["etsy_listing_id"], "stage": r["stage"], "title": title, "keywords": items})
        print(f"{r['etsy_listing_id']} [{r['stage']}] {title[:60]}")
        for k in items:
            print(f"    - {k['keyword']}  ({k['source']})")
        if not a.dry_run:
            db.req("PATCH", f"product_library?id=eq.{r['id']}&keywords_edited_at=is.null", {"amazon_keywords": items}, "return=minimal")
            if r.get("job_id") and an:
                db.req("PATCH", f"analyses?job_id=eq.{urllib.parse.quote(r['job_id'])}", {"keyword_items": items}, "return=minimal")
    if a.out:
        json.dump(report, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(f"done: {len(report)} 条{'（dry-run 未写入）' if a.dry_run else '已写入'}；使用图片描述 {img_used} 条；LLM 调用 {llm.calls} 次 / {llm.units_used} tokens")


if __name__ == "__main__":
    main()
