import re
import urllib.error
import urllib.parse
import urllib.request

_RE = re.compile(r"(?:^|//|\.)etsy\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?listing/(\d+)", re.I)
_SHORT = re.compile(r"^https?://(www\.)?etsy\.me/[A-Za-z0-9]+", re.I)


def parse_listing_id(url: str):
    """从 Etsy 链接（www./m./无前缀，含地区前缀）中解析 listing id；不合法返回 None。"""
    m = _RE.search(url or "")
    return int(m.group(1)) if m else None


def parse_slug(url: str):
    m = re.search(r"/listing/\d+/([^/?#]+)", url or "")
    return m.group(1) if m else None


def is_short(url: str):
    return bool(_SHORT.match((url or "").strip()))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def resolve_short(url, opener=None, max_hops=5, timeout=15):
    """展开 etsy.me 短链：逐跳读取 Location（不跟随到 etsy.com 页面），返回含 listing id 的 URL 或 None。"""
    opener = opener or urllib.request.build_opener(_NoRedirect)
    cur = url
    for _ in range(max_hops):
        if parse_listing_id(cur):
            return cur
        req = urllib.request.Request(cur, method="HEAD", headers={"User-Agent": "Mozilla/5.0 etsy-analyzer"})
        try:
            r = opener.open(req, timeout=timeout)
            loc = r.headers.get("Location")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if 300 <= e.code < 400 else None
        except (urllib.error.URLError, OSError):
            return None
        if not loc:
            return None
        cur = urllib.parse.urljoin(cur, loc)
    return cur if parse_listing_id(cur) else None
