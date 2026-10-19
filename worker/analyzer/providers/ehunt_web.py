"""EHunt 网页回退（ETSY_PROVIDER=ehunt_web）：EHunt API 无订阅（免费 200 credits 已用完）时的预筛选取数。
ECS 写请求到 Supabase ehunt_lookup_requests（033）→ box 上已登录 EHunt 网页的 selection worker 按 listing id 搜索，
只读取【主图 URL】一项写回（不读标题/价格/销量/收藏/评论）→ 这里轮询取回，返回只有 image_url 的 EtsyProduct。

- 缓存：先查 API 的 24h 缓存 ehunt:listing:<id>（以前 API 取到的完整数据，直接复用），再查 ehunt_web:listing:<id>（24h）。
- EHUNT_WEB_PREFER_API=1：先试 API（买了 API 套餐后），API 服务异常/额度用完 → 自动回退网页；API 结果照常写缓存。
- 等待：最多 EHUNT_WEB_TIMEOUT_SECONDS（默认 90 秒）；仍在排队 → EHuntWebPending（回队列 1 分钟，不计尝试次数）。
- box 没响应（排队超过 EHUNT_WEB_STALE_SECONDS，默认 600 秒仍无人认领）/ EHunt 登录失效 → EHuntServiceError（熔断 30 分钟，不计尝试次数）。
"""
import logging
import time
from datetime import datetime, timezone
from dataclasses import asdict
from .base import EtsyProvider
from .ehunt import EHuntEtsy, EHuntQuotaError, EHuntServiceError, ANOMALY_PREFIX, NOT_FOUND
from ..models import EtsyProduct
from ..errors import PermanentError
from ..http import HttpError

log = logging.getLogger("ehunt_web")
LOGIN_TAG = "EHunt 未登录"
SOURCE_NOTE = "EHunt 网页（仅主图）"


class EHuntWebPending(Exception):
    """box 还没处理完：任务回队列稍后再试（pipeline 的 rate_limited 分支：不熔断、不计尝试次数）。"""
    rate_limited = True


def _ts(v):
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class EHuntWebEtsy(EtsyProvider):
    name = "ehunt_web"
    no_review_text = True

    def __init__(self, store, cache=None, ttl=86400, timeout=90, poll=3, stale=600, api=None, sleep=time.sleep, now=None):
        if store is None or not hasattr(store, "lookup_request"):
            raise ValueError("ehunt_web 需要 Supabase store（033 ehunt_lookup_requests）")
        self.store, self.cache, self.ttl, self.timeout, self.poll, self.stale, self.api = store, cache, ttl, timeout, poll, stale, api
        self._sleep, self._now = sleep, now or (lambda: datetime.now(timezone.utc))
        self.calls, self.cache_hits, self.web_lookups = 0, 0, 0   # calls 只统计 API（provider_usage_daily 的 ehunt 行）
        self.used_today = self.remaining = None
        self._totals = {}

    # -- 对 pipeline 透明：额度字段/评论总数跟随 API（若启用） --
    def _sync_api(self):
        if self.api:
            self.calls, self.used_today, self.remaining = self.api.calls, self.api.used_today, self.api.remaining
            self._totals.update(self.api._totals)

    def get_listing(self, listing_id, slug=None):
        lid = int(listing_id)
        hit = self.cache.get(f"ehunt:listing:{lid}") if self.cache else None
        if hit is not None:   # API 时期的完整缓存：直接复用（0 次调用）
            self.cache_hits += 1
            if hit.get("not_found"):
                raise PermanentError(f"{NOT_FOUND}（EHunt 未返回 listing {lid}，24 小时内缓存结果）")
            p = EtsyProduct(**hit)
            p.raw = {**(p.raw or {}), "cache": True}
            self._totals[lid] = ((p.raw or {}).get("ehunt") or {}).get("reviews")
            return p
        hit = self.cache.get(f"ehunt_web:listing:{lid}") if self.cache else None
        if hit is not None:
            self.cache_hits += 1
            if hit.get("not_found"):
                raise PermanentError(f"{NOT_FOUND}（EHunt 网页搜不到 listing {lid}，24 小时内缓存结果）")
            return self._product(lid, hit.get("image_url"), cached=True)
        if self.api:
            try:
                p = self.api.get_listing(lid, slug=slug)
                self._sync_api()
                return p
            except (EHuntServiceError, EHuntQuotaError) as e:
                self._sync_api()
                log.warning("EHunt API unavailable for %s (%s); falling back to web lookup", lid, str(e)[:120])
        return self._web(lid)

    def _product(self, lid, image_url, cached=False):
        raw = {"provider": "ehunt_web", "source": SOURCE_NOTE, "image_only": True}
        if cached:
            raw["cache"] = True
        return EtsyProduct(listing_id=lid, title="", description="", price=None, currency="USD", tags=[],
                           shop_name="", shop_id=None, image_url=image_url or "", num_favorers=None, raw=raw)

    def _web(self, lid):
        t0 = time.monotonic()
        row = self.store.lookup_request(lid) or {}
        while True:
            st = row.get("status")
            if st == "done":
                self.web_lookups += 1
                if self.cache:
                    self.cache.put(f"ehunt_web:listing:{lid}", "ehunt_web", {"image_url": row.get("image_url")}, self.ttl)
                self._clear_anomaly()
                return self._product(lid, row.get("image_url"))
            if st == "not_found":
                self.web_lookups += 1
                if self.cache:
                    self.cache.put(f"ehunt_web:listing:{lid}", "ehunt_web", {"not_found": True}, self.ttl)
                self._clear_anomaly()
                raise PermanentError(f"{NOT_FOUND}（EHunt 网页搜不到 listing {lid}）")
            if st == "login_expired":
                raise EHuntServiceError(f"{ANOMALY_PREFIX}（{LOGIN_TAG}：需要在 box 上运行 deploy/box-selection-worker.sh login 重新登录）", LOGIN_TAG)
            if st == "error":
                raise HttpError(f"EHunt 网页取数失败：{str(row.get('error') or '')[:200]}")
            if st == "queued":
                req = _ts(row.get("requested_at"))
                if req and (self._now() - req).total_seconds() > self.stale:
                    raise EHuntServiceError(f"{ANOMALY_PREFIX}（box 取数 worker 未响应：请求排队超过 {self.stale // 60} 分钟无人处理）",
                                            "box worker 未响应")
            if time.monotonic() - t0 >= self.timeout:
                raise EHuntWebPending(f"EHunt 网页取数排队中（box 处理中），稍后自动继续")
            self._sleep(self.poll)
            row = self.store.lookup_get(lid) or row

    def _clear_anomaly(self):
        """网页取数成功 → 清除熔断与横幅（API 时期留下的 'daily_limit' 异常、之前的未登录/box 未响应）。
        熔断期间 claim_next_job_v2 不领取预筛选取数任务，所以网页恢复后必须立即解除。"""
        try:
            s = self.store.get_service_status() or {}
            if s.get("ehunt_paused_until") or str(s.get("reason") or "").startswith(ANOMALY_PREFIX):
                self.store.set_service_status(ehunt_paused_until=None, reason=None)
        except Exception:  # noqa: BLE001
            log.warning("clear anomaly failed", exc_info=True)

    def fetch_listing_reviews(self, listing_id, limit, offset):
        return [], self._totals.get(int(listing_id))
