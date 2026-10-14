import re

_RE = re.compile(r"etsy\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?listing/(\d+)", re.I)


def parse_listing_id(url: str):
    """从 Etsy 链接中解析 listing id；不合法返回 None。"""
    m = _RE.search(url or "")
    return int(m.group(1)) if m else None
