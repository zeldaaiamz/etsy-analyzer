"""选品雷达 × 三步查同一张「不做的品类」表（Hongyan 2026-11-19 15:00「同步更新到选品雷达里」）。

规则只有一份：ECS sif-root-finder 的 rootpool.sqlite3 → exclude_rules 表（带电设备 / 人用保健品 / 眼镜 / 成人服装 / IP / 大件 + 保留例外，
纸类宁可多留）。这里不存任何词表，只调 ECS POST /api/category-check（服务令牌 X-RF-Service-Token）。

环境变量（worker.env）：
  RF_CATEGORY_URL    例 https://47.98.137.124/api/category-check；不配 = 整个闸门关闭（和现在完全一样）
  RF_CATEGORY_TOKEN  ECS config 里 CATEGORY_CHECK_TOKEN 的同一个值
  SEL_CATEGORY_KEYWORD_MODE  block（默认：命中的关键词不查 EHunt、不调视觉，run 标失败并写原因）| log（只记日志）| off
  SEL_CATEGORY_TITLE_MODE    off（默认）| log（只记日志，不改结论）| exclude（命中直接按规则排除，不调视觉）

任何失败（没配置、超时、ECS 报错、返回格式不对）一律放行 —— 闸门只会省钱，不会让选品停下。
"""
import json, logging, os, time, urllib.request

log = logging.getLogger("selection.category_gate")
TIMEOUT = float(os.environ.get("SEL_CATEGORY_TIMEOUT", "6"))
_FAILS = {"n": 0, "until": 0.0}          # 连续失败 3 次 → 10 分钟内不再调用（避免每条商品都等超时）
_CACHE = {}


def _cfg():
    return os.environ.get("RF_CATEGORY_URL", "").strip(), os.environ.get("RF_CATEGORY_TOKEN", "").strip()


def _mode(name, default, allowed):
    v = os.environ.get(name, default).strip().lower()
    return v if v in allowed else default


def keyword_mode(): return _mode("SEL_CATEGORY_KEYWORD_MODE", "block", ("block", "log", "off"))
def title_mode(): return _mode("SEL_CATEGORY_TITLE_MODE", "off", ("off", "log", "exclude"))


def _post(kind, texts, opener=None):
    url, tok = _cfg()
    if not url or not tok or time.monotonic() < _FAILS["until"]: return None
    body = json.dumps({kind: list(texts)}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json", "X-RF-Service-Token": tok})
    try:
        if opener: raw = opener(req, TIMEOUT)
        else:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r: raw = r.read()
        data = json.loads(raw)
        res = data.get("results")
        if not isinstance(res, list) or len(res) != len(texts): raise ValueError("bad response")
        _FAILS["n"] = 0
        return res
    except Exception as e:
        _FAILS["n"] += 1
        if _FAILS["n"] >= 3: _FAILS["until"] = time.monotonic() + 600; _FAILS["n"] = 0
        log.warning("category-check 失败，放行：%s", str(e)[:160])
        return None


def check_keyword(keyword, opener=None):
    """→ None（放行）或 {'reason','detail','mode'}。只有 action=drop 才挡；doubt（可能纸类等）一律放行。"""
    mode = keyword_mode()
    if mode == "off" or not str(keyword or "").strip(): return None
    res = _post("keywords", [str(keyword)], opener)
    if not res or res[0].get("action") != "drop": return None
    hit = {"reason": res[0].get("reason") or "", "detail": res[0].get("detail") or "", "mode": mode}
    log.info("category gate keyword %r → %s（%s）mode=%s", keyword, hit["reason"], hit["detail"], mode)
    return hit if mode == "block" else None


def block_message(hit):
    return f"不做的品类（{hit['reason']}）：这个关键词按三步查同一张排除品类表跳过，没有查 EHunt、没有调视觉。如需放行请在排除品类表加保留例外。"[:300]


def title_rule(title, opener=None):
    """→ None 或 classify.rule 同格式 (结果, 原因, 说明, 定制类型)。log 模式只记日志返回 None。"""
    mode = title_mode()
    if mode == "off" or not str(title or "").strip(): return None
    key = str(title)
    if key in _CACHE: res = _CACHE[key]
    else:
        r = _post("titles", [key], opener); res = r[0] if r else None
        if r is not None:
            if len(_CACHE) > 5000: _CACHE.clear()
            _CACHE[key] = res
    if not res or res.get("action") != "drop": return None
    log.info("category gate title %r → %s（%s）mode=%s", key[:80], res.get("reason"), res.get("detail"), mode)
    if mode != "exclude": return None
    return ("排除", "不做的品类", f"{res.get('reason') or ''}：{res.get('detail') or ''}"[:200], "无")
