import json
import urllib.request
import urllib.parse
import urllib.error


class HttpError(Exception):
    pass


def _open(req, timeout):
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read()[:500].decode(errors='replace')
        raise HttpError(f"HTTP {e.code} {req.get_method()} {req.full_url.split('?')[0]}: {body}") from None


def request_json(method, url, headers=None, params=None, body=None, timeout=60):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", **(headers or {})})
    with _open(req, timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def request_bytes(method, url, headers=None, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with _open(req, timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")
