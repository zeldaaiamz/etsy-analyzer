"""错误分类：哪些可重试（网络/超时/5xx/429），哪些不可重试（找不到、4xx、业务错误）。"""
import json
import re


class PermanentError(Exception):
    """不可重试的错误（如 URL 非法、商品不存在）。"""


class JobCanceled(Exception):
    """用户请求取消，worker 在阶段之间检测到。"""


_CODE = re.compile(r"HTTP (\d{3})")


def is_retryable(e, attempts=1):
    """返回 True 表示值得重试（仍受 max_attempts 限制）。"""
    if isinstance(e, (PermanentError, JobCanceled)) or type(e).__name__ in ("EHuntQuotaError", "EHuntServiceError"):
        return False
    if type(e).__name__ == "HttpError":
        m = _CODE.search(str(e))
        if m:
            c = int(m.group(1))
            return c >= 500 or c in (408, 425, 429)
        return "错误" not in str(e)  # EHunt/Keepamore 业务错误（code != 200/0000）不重试；无状态码的网络异常重试
    if isinstance(e, (json.JSONDecodeError, KeyError, ValueError, TypeError, IndexError)):
        return attempts < 2  # 解析失败（如 LLM 返回坏 JSON）最多重试 1 次
    return True  # 超时 / 连接错误 / 未知
