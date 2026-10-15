#!/usr/bin/env python3
"""PM-001 一次性回填：为已在产品库的产品生成「一键亚马逊搜索」关键词（只写 DB，不改线上 UI）。
数据来源：产品库快照 + 关联任务的 etsy_products（标题/标签/描述/评论）+ analyses（旧搜索词）。
主图：DOUBAO_VISION_ENABLED=1 时用豆包视觉读主图（物品/场景/产品上的文案），DeepSeek 按五个方向生成。
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
- old_keywords 是之前分析给出的搜索词，可参考。image_text 是豆包视觉从主图上逐字读出的文字（D3 只能用它或标题里的文案），
  image_keyword_hints 是豆包给的看图搜索词提示。"""


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
    ap.add_argument("--force", action="store_true", help="已有关键词也重新生成（人工编辑过的永远跳过）")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    env = load_env(a.env)
    from analyzer.providers.doubao_vision import from_env
    from analyzer.providers.llm_openai import main_image_url
    db = Rest(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    llm = OpenAICompatLLM(env.get("LLM_BASE_URL", "https://api.deepseek.com"), env["LLM_API_KEY"], env.get("LLM_MODEL", "deepseek-chat"))
    vision = from_env(env)
    print(f"豆包视觉：{'启用 ' + vision.model + '（' + vision.mode + '）' if vision else '未启用（仅标题）'}")
    rows = db.req("GET", "product_library?select=id,job_id,etsy_listing_id,title,image_url,keywords,product_summary,stage,amazon_keywords,keywords_edited_at")
    report = []
    for r in rows:
        if r.get("keywords_edited_at") or (r.get("amazon_keywords") and not a.force):
            continue
        ep, an = {}, {}
        if r.get("job_id"):
            q = urllib.parse.quote(r["job_id"])
            ep = (db.req("GET", f"etsy_products?job_id=eq.{q}&select=title,description,tags,materials,raw,reviews,image_url") or [{}])[0]
            an = (db.req("GET", f"analyses?job_id=eq.{q}&select=keywords,product_summary") or [{}])[0]
        title = ep.get("title") or r.get("title") or ""
        p = EtsyProduct(listing_id=r["etsy_listing_id"], title=title, description=ep.get("description") or "",
                        tags=ep.get("tags") or [], materials=ep.get("materials") or [], raw=ep.get("raw") or {},
                        image_url=ep.get("image_url") or r.get("image_url") or "")
        revs = [x.get("text", "")[:160] for x in (ep.get("reviews") or []) if isinstance(x, dict) and x.get("text")][:15]
        old = an.get("keywords") or r.get("keywords") or []
        info = {"title": title, "tags": p.tags[:13], "materials": p.materials[:8], "description": p.description[:1500],
                "product_summary": an.get("product_summary") or r.get("product_summary") or "",
                "old_keywords": old, "review_phrases": revs}
        v = vision.describe(main_image_url(p), title) if vision else None
        img = image_description(p) or (v or {}).get("description", "")
        if img:
            info["image_description"] = img
        if v:
            info["image_text"] = v.get("text_on_product") or []
            info["image_keyword_hints"] = v.get("keywords") or []
        j = {}
        for _ in range(2):  # 偶发非法 JSON：重试一次，仍失败则兜底（D1 = 旧搜索词 / 原标题核心短语）
            try:
                j = llm._chat(PROMPT, info)
                break
            except ValueError as e:
                print(f"    ! LLM JSON 解析失败，重试：{str(e)[:60]}")
        items = akw.build(j.get("amazon_keywords"), title, fallback=old, image_text=(v or {}).get("text_on_product"))
        report.append({"id": r["id"], "listing_id": r["etsy_listing_id"], "stage": r["stage"], "title": title,
                       "vision": v, "keywords": items})
        print(f"{r['etsy_listing_id']} [{r['stage']}] {title[:60]}  vision={'ok' if v else '—'}  text={((v or {}).get('text_on_product'))}")
        for k in items:
            print(f"    - D{k['direction']} {akw.DIRECTIONS[k['direction']]}: {k['keyword']}  ({k['source']})")
        if not a.dry_run:
            db.req("PATCH", f"product_library?id=eq.{r['id']}&keywords_edited_at=is.null", {"amazon_keywords": items}, "return=minimal")
            if r.get("job_id") and an:
                db.req("PATCH", f"analyses?job_id=eq.{urllib.parse.quote(r['job_id'])}", {"keyword_items": items}, "return=minimal")
    if a.out:
        json.dump(report, open(a.out, "w"), ensure_ascii=False, indent=1)
    dirs = {}
    for x in report:
        for k in x["keywords"]:
            dirs[k["direction"]] = dirs.get(k["direction"], 0) + 1
    print(f"done: {len(report)} 条{'（dry-run 未写入）' if a.dry_run else '已写入'}；方向分布 {dict(sorted(dirs.items()))}；"
          f"image 来源 {sum(any(k['source'] == 'image' for k in x['keywords']) for x in report)} 个产品；"
          f"DeepSeek {llm.calls} 次 / {llm.units_used} tokens")
    if vision:
        print(f"豆包：{vision.calls} 次，失败 {vision.failures}；tokens 输入 {vision.prompt_tokens} / 输出 {vision.completion_tokens}；估算 ¥{vision.cost_cny}")


if __name__ == "__main__":
    main()
