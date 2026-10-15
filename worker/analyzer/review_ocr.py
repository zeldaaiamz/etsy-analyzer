"""评论截图 OCR（本地 tesseract，不调外部视觉 API）。
Etsy 评论弹窗每页 8 条，日期在评论人名字旁：'Sally Oct 13, 2026'；也支持中文界面 '2026年10月13日'。
- parse_reviews(text) → [(reviewer_key, date)]；parse_total(text) → 评论总数或 None
- extract(texts) → (日期列表（按评论人+日期跨截图去重，新→旧）, 总数)
- run_once(cfg)：领取 review_ocr_jobs 一条 → 下载私有截图 → OCR → 回写 extracted_dates（status=done/failed）。
  用户在前端核对后才通过 confirm_review_dates 写入 listing_reviews(source='screenshot')。
默认不在主循环启用（REVIEW_OCR_ENABLED=1 时由 main 调用 / 或 `python -m analyzer.review_ocr`），依赖见 ARCHITECTURE.md。
"""
import datetime as dt
import logging
import re

log = logging.getLogger("review_ocr")
MON = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
# OCR 常见误识：O→0、l/I→1；逗号可能丢失
_EN = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+([0-3OolI]?[0-9OolI])\s*[,.]?\s+([12][0-9OolI]{3})\b", re.I)
_ZH = re.compile(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_TOTAL = re.compile(r"(?:(?:reviews?|评论)\s*(?:for\s*this\s*(?:item|shop))?\s*[(（]\s*([\d,，]+)\s*[)）]|([\d,，]+)\s*(?:reviews|条评论|个评论|则评论))", re.I)


def _digits(s):
    return int(re.sub(r"[^\d]", "", s.replace("O", "0").replace("o", "0").replace("l", "1").replace("I", "1")))


def _mk(y, m, d, today):
    try:
        x = dt.date(y, m, d)
    except ValueError:
        return None
    return x if dt.date(2005, 1, 1) <= x <= today else None


def parse_reviews(text, today=None):
    today = today or dt.date.today()
    out = []
    for line in str(text or "").splitlines():
        for rx, conv in ((_EN, lambda g: (_digits(g[2]), MON[g[0][:3].lower()], _digits(g[1]))),
                         (_ZH, lambda g: (int(g[0]), int(g[1]), int(g[2])))):
            for mt in rx.finditer(line):
                d = _mk(*conv(mt.groups()), today)
                if d:
                    name = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", line[:mt.start()].lower())[-24:]
                    out.append((name, d))
    return out


def parse_total(text):
    best = None
    for mt in _TOTAL.finditer(str(text or "")):
        n = int(re.sub(r"[^\d]", "", mt.group(1) or mt.group(2)))
        best = n if best is None else max(best, n)
    return best


def extract(texts, today=None):
    """多张截图（可能重叠）：同一评论人+日期只算一次；无评论人名时同日期按单张截图内的最大出现次数合并。"""
    named, anon = set(), {}
    for t in texts:
        per = {}
        for name, d in parse_reviews(t, today):
            if name:
                named.add((name, d))
            else:
                per[d] = per.get(d, 0) + 1
        for d, c in per.items():
            anon[d] = max(anon.get(d, 0), c)
    dates = [d for _, d in named] + [d for d, c in anon.items() for _ in range(c)]
    totals = [x for x in (parse_total(t) for t in texts) if x is not None]
    return sorted(dates, reverse=True), (max(totals) if totals else None)


def ocr_image(data):
    import io
    import pytesseract  # apt: tesseract-ocr tesseract-ocr-chi-sim；pip: pytesseract pillow
    from PIL import Image, ImageOps
    img = ImageOps.grayscale(Image.open(io.BytesIO(data)))
    if img.width < 1600:
        img = img.resize((img.width * 2, img.height * 2))
    return pytesseract.image_to_string(img, lang="eng+chi_sim")


def run_once(cfg, ocr=ocr_image):
    from .http import request_json, HttpError
    import urllib.request
    h = {"apikey": cfg.supabase_key, "Authorization": f"Bearer {cfg.supabase_key}", "Content-Type": "application/json"}
    rows = request_json("POST", f"{cfg.supabase_url}/rest/v1/rpc/claim_review_ocr", h, body={}) or []
    if not rows:
        return False
    j = rows[0]
    patch = {"processed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    try:
        texts = []
        for p in j["image_paths"]:
            req = urllib.request.Request(f"{cfg.supabase_url}/storage/v1/object/review-shots/{p}", headers=h)
            with urllib.request.urlopen(req, timeout=60) as r:
                texts.append(ocr(r.read()))
        dates, total = extract(texts)
        patch.update(status="done", extracted_dates=[d.isoformat() for d in dates], total_reviews=total,
                     ocr_error=None if dates else "未识别到评论日期（请确认截图包含评论列表，或手动填写）")
    except (HttpError, OSError, ImportError) as e:
        patch.update(status="failed", ocr_error=str(e)[:300])
    request_json("PATCH", f"{cfg.supabase_url}/rest/v1/review_ocr_jobs", {**h, "Prefer": "return=minimal"}, params={"id": f"eq.{j['id']}"}, body=patch)
    log.info("review OCR job %s → %s (%s dates)", j["id"], patch["status"], len(patch.get("extracted_dates") or []))
    return True


if __name__ == "__main__":
    from .config import Config
    logging.basicConfig(level=logging.INFO)
    c = Config.from_env()
    while run_once(c):
        pass
