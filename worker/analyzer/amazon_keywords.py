"""PM-001 一键亚马逊搜索：按「五个方向」生成的关键词（入库时随 DeepSeek 分析生成，主图由豆包视觉辅助）。

结构：[{"keyword": "retro phone camera magnet", "direction": 1, "source": "title|image|review|manual", "basis": "依据"}]
方向（Hongyan 2026-10-15 批准）：
  D1 精准词（必需，恰好 1 个，最具体、保留核心主题，搜了就知道亚马逊有没有同款）
  D2 同场景相似品  D3 产品文案/卖点词（印在产品上的口号，用来找同款）  D4 人群礼物词  D5 细分场景词（场景 + 人群）
规则：≤5 个、去重（含近似重复）；不是每个产品都要五个方向齐全；D3 必须在标题或图片识别出的文字里出现过（不能编）。
旧数据的 source=original-title 仍被接受（视为 title 来源）。
"""
import re

SOURCES = ("title", "image", "review", "original-title", "manual")
DIRECTIONS = {1: "精准词", 2: "同场景相似品", 3: "产品文案/卖点词", 4: "人群礼物词", 5: "细分场景词"}
MAX_KEYWORDS = 5
MAX_ORIG_WORDS = 4
_BAD = {"handmade", "custom", "personalized", "etsy", "listing", "gift", "gifts", "set", "of", "for", "with", "and", "the", "a"}
# 常见商标/品牌词：关键词里出现即丢弃（侵权风险；豆包会把产品上的品牌 logo 读成“文案”）
BRANDS = {"kodak", "polaroid", "fujifilm", "fuji", "canon", "nikon", "leica", "disney", "marvel", "pokemon", "nintendo", "lego",
          "hasbro", "heroquest", "mtg", "wotc", "starbucks", "stanley", "barbie", "harry potter", "taylor swift", "nfl", "nba"}
_STOP = {"for", "with", "and", "the", "a", "an", "of", "in", "on", "to", "by", "from", "set"}

PROMPT_FIELD = (
    '"amazon_keywords": [{"keyword": "英文亚马逊搜索词", "direction": 1-5, "source": "title | image | review",'
    ' "basis": "中文依据（引用标题/图片描述/产品上文字/评论的哪部分）"}]'
)
PROMPT_RULES = """- amazon_keywords：最多 5 个，按下面五个方向挑选，不是每个产品都要五个方向齐全，按产品实际选择；去重（单复数/词序不同视为重复）。
  D1 精准词（必须有且只有 1 个，排第一）：最精准、最细分的产品说法，保留产品核心主题，在亚马逊一搜就能判断有没有同款。
     例：retro phone camera magnet，而不是 camera magnet。
  D2 同场景相似品：同一使用场景、功能相近但不是同一种产品。例：布道笔记本 → church notes。
  D3 产品文案/卖点词：产品上印的口号/文案原文（来自 image_text 或标题），用于在亚马逊找同款。例：record your life。
     只能逐字使用 image_text 或标题里出现的文案；没有文案就不要 D3。
  D4 人群礼物词：送给特定人群的礼物词。例：christian gift。
  D5 细分场景词：细分场景 + 人群。例：church supplies for women。
  source：依据来自标题/标签/描述用 title，来自 image_description / image_text 用 image，来自评论用 review。
  D3 不能是品牌/商标名（如产品上印的 Kodak、Polaroid logo）。
  每个词都必须有依据，不得凭空生成；没有图片信息就不要使用 image 来源；不含品牌/店铺名/handmade/custom/etsy。"""


def norm(s):
    s = re.sub(r"[^a-z0-9 '&-]+", " ", str(s or "").lower().replace("\u2019", "'"))
    return re.sub(r"\s+", " ", s).strip()


def _stem(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("es") and w[-3] in "sxz":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def dedup_key(kw):
    """近似重复判定：小写、去标点、单复数归一、去停用词、忽略词序。"""
    return " ".join(sorted(_stem(w) for w in norm(kw).split() if w not in _STOP)) or norm(kw)


def core_phrase(title):
    """兜底：从原标题取核心名词短语（第一段里最后 2-3 个有效词，原文连续片段）。"""
    seg = re.split(r"\s[-–—|:,(]\s?|[,|(:–—]|-\s", str(title or ""))[0]
    words = [w for w in norm(seg).split()]
    while words and words[-1] in _BAD:
        words.pop()
    if not words:
        return ""
    tail = words[-3:] if len(words) >= 3 else words
    while len(tail) > 1 and tail[0] in _BAD:
        tail = tail[1:]
    return " ".join(tail)


def _in_title(kw, title):
    return f" {norm(kw)} " in f" {norm(title)} "


_GENERIC = _BAD | {"cute", "beautiful", "unique", "vintage", "new", "best", "cards", "card", "item", "product", "design", "art",
                   "box", "variety", "premium", "collectible", "funny", "special", "sale", "bundle", "pack", "piece", "one", "kit"}


def title_unclear(title):
    """标题是否不足以判断产品：有效（非泛化、非数字）英文词 < 2 个，或标题过短。"""
    words = [w for w in norm(re.split(r"[,|(:–—]", str(title or ""))[0]).split() if w not in _GENERIC and not w.isdigit()]
    return len(words) < 2 or len(norm(title)) < 12


def _dir(v):
    try:
        d = int(str(v).strip().lstrip("Dd"))
    except (TypeError, ValueError):
        return None
    return d if d in DIRECTIONS else None


def build(items, title, fallback=None, limit=MAX_KEYWORDS, image_text=None):
    """校验 + 规范化 + 合并 LLM 输出。
    items：LLM 的 amazon_keywords；fallback：旧版纯字符串关键词（D1 缺失时兜底）；image_text：豆包识别出的产品上文字（校验 D3）。
    输出：D1 恰好 1 个排第一；其余优先每个方向 1 个（按方向顺序），再用同方向的其它词补满 ≤limit。"""
    allowed_text = " ".join([norm(title)] + [norm(t) for t in (image_text or [])])
    cands, seen = [], set()
    for it in items or []:
        if isinstance(it, str):
            it = {"keyword": it}
        if not isinstance(it, dict):
            continue
        k = norm(it.get("keyword"))
        src = str(it.get("source") or "title").strip().lower().replace("_", "-")
        src = {"original-title": "title", "original": "title", "reviews": "review", "images": "image", "manual": "title"}.get(src, src)
        d = _dir(it.get("direction"))
        if not k or len(k) > 80 or src not in SOURCES or d is None:
            continue
        if d == 3 and any(f" {b} " in f" {k} " for b in BRANDS):
            continue  # 产品上的品牌 logo 不算卖点文案
        if d == 3 and f" {k} " not in f" {allowed_text} ":
            continue  # D3 文案必须逐字出现在标题或图片文字里
        dk = dedup_key(k)
        if dk in seen:
            continue
        seen.add(dk)
        cands.append({"keyword": k, "direction": d, "source": src, "basis": str(it.get("basis") or "")[:200]})
    d1 = [c for c in cands if c["direction"] == 1]
    if not d1:  # 兜底：旧搜索词第一个 / 原标题核心短语
        for kw in list(fallback or []) + [core_phrase(title)]:
            k = norm(kw)
            if k and dedup_key(k) not in seen:
                d1 = [{"keyword": k, "direction": 1, "source": "title", "basis": "兜底：旧分析搜索词 / 原标题核心短语"}]
                seen.add(dedup_key(k))
                break
    rest = [c for c in cands if c["direction"] != 1]
    first = {}
    for c in rest:
        first.setdefault(c["direction"], c)
    ordered = d1[:1] + [first[d] for d in sorted(first)] + [c for c in rest if c is not first.get(c["direction"])]
    return ordered[:limit]


def parse_manual(items, limit=MAX_KEYWORDS):
    """前端人工编辑后的列表校验（与 RPC update_library_keywords 规则一致）。"""
    out, seen = [], set()
    for it in items or []:
        k = norm(it.get("keyword") if isinstance(it, dict) else it)
        src = (it.get("source") if isinstance(it, dict) else None) or "manual"
        if not k or src not in SOURCES or dedup_key(k) in seen:
            continue
        seen.add(dedup_key(k))
        d = _dir(it.get("direction")) if isinstance(it, dict) else None
        out.append({"keyword": k, "source": src, "basis": (it.get("basis") if isinstance(it, dict) else "") or "",
                    **({"direction": d} if d else {})})
    if len(out) > limit:
        raise ValueError(f"最多 {limit} 个关键词")
    return out


def search_url(kw):
    from urllib.parse import quote
    return "https://www.amazon.com/s?k=" + quote(kw, safe="-_.!~*'()")
