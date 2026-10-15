"""PM-001 一键亚马逊搜索：带来源的关键词（入库时随 DeepSeek 分析生成）。

结构：[{"keyword": "church note", "source": "title|image|review|original-title|manual", "basis": "依据"}]
规则：≤5 个、去重（含近似重复）、恰好 1 个 original-title（Etsy 原标题里的核心名词短语，必须是原标题的连续片段）。
"""
import re

SOURCES = ("title", "image", "review", "original-title", "manual")
MAX_KEYWORDS = 5
MAX_ORIG_WORDS = 4  # 原标题核心名词短语最长 4 个词
_BAD = {"handmade", "custom", "personalized", "etsy", "listing", "gift", "gifts", "set", "of", "for", "with", "and", "the", "a"}
_STOP = {"for", "with", "and", "the", "a", "an", "of", "in", "on", "to", "by", "from", "set"}

PROMPT_FIELD = (
    '"amazon_keywords": [{"keyword": "英文亚马逊搜索词，2-4 个单词，优先“使用场景/主题 + 品类名”，如 church note、summer note",'
    ' "source": "title | image | review | original-title", "basis": "中文依据（引用了标题/图片描述/评论里的哪部分）"}]'
)
PROMPT_RULES = """- amazon_keywords：最多 5 个，去重（单复数/词序不同视为重复）。来源优先级 title > image > review；
  只有标题不足以判断产品时才用 image（输入中的 image_description），评论里买家的口语叫法可作 review 来源补充。
  必须恰好 1 个 source=original-title：从 Etsy 原标题中逐字截取的一个核心名词短语（原文连续片段，不改词，2-4 个词）。
  每个词都必须有依据，不得凭空生成；没有图片描述就不要使用 image 来源。"""


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


def build(items, title, fallback=None, limit=MAX_KEYWORDS):
    """校验 + 规范化 LLM 输出。items：LLM 的 amazon_keywords（可能不规范）；fallback：旧版纯字符串关键词（来源记 title）。"""
    out, seen, orig = [], set(), None
    def add(kw, src, basis=""):
        k = norm(kw)
        if not k or len(k) > 80 or src not in SOURCES:
            return False
        d = dedup_key(k)
        if d in seen:
            return False
        seen.add(d)
        out.append({"keyword": k, "source": src, "basis": str(basis or "")[:200]})
        return True
    for it in items or []:
        if isinstance(it, str):
            it = {"keyword": it, "source": "title"}
        if not isinstance(it, dict):
            continue
        src = str(it.get("source") or "").strip().lower().replace("_", "-")
        src = {"original": "original-title", "originaltitle": "original-title", "reviews": "review", "images": "image"}.get(src, src)
        if src == "manual":
            src = "title"  # LLM 不能产出 manual
        if src == "original-title":
            if orig is None and _in_title(it.get("keyword"), title) and len(norm(it.get("keyword")).split()) <= MAX_ORIG_WORDS:
                orig = (norm(it.get("keyword")), str(it.get("basis") or "Etsy 原标题逐字片段"))
            continue
        add(it.get("keyword"), src, it.get("basis"))
    for kw in fallback or []:
        add(kw, "title", "旧版分析搜索词（依据标题/标签）")
    if orig is None:
        cp = core_phrase(title)
        if cp:
            orig = (cp, "Etsy 原标题核心名词短语（自动截取）")
    res = []
    if orig:
        d = dedup_key(orig[0])
        # 原标题短语若与已有词重复：保留 original-title 这条，去掉重复的那条
        out = [x for x in out if dedup_key(x["keyword"]) != d]
        res = out[:limit - 1]
        res.append({"keyword": orig[0], "source": "original-title", "basis": orig[1][:200]})
    else:
        res = out[:limit]
    return res


def parse_manual(items, limit=MAX_KEYWORDS):
    """前端人工编辑后的列表校验（与 RPC update_library_keywords 规则一致）。"""
    out, seen = [], set()
    for it in items or []:
        k = norm(it.get("keyword") if isinstance(it, dict) else it)
        src = (it.get("source") if isinstance(it, dict) else None) or "manual"
        if not k or src not in SOURCES or dedup_key(k) in seen:
            continue
        seen.add(dedup_key(k))
        out.append({"keyword": k, "source": src, "basis": (it.get("basis") if isinstance(it, dict) else "") or ""})
    if len(out) > limit:
        raise ValueError(f"最多 {limit} 个关键词")
    return out


def search_url(kw):
    from urllib.parse import quote
    return "https://www.amazon.com/s?k=" + quote(kw, safe="-_.!~*'()")
