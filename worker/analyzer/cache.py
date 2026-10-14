"""供应商结果缓存（EHunt listing、Keepamore 搜索/商品）。SupabaseStore 用 provider_cache 表，测试用内存。"""
import time


class MemoryCache:
    def __init__(self):
        self.d = {}

    def get(self, key):
        v = self.d.get(key)
        return v[1] if v and v[0] > time.time() else None

    def get_many(self, keys):
        return {k: v for k in keys if (v := self.get(k)) is not None}

    def put(self, key, provider, payload, ttl):
        self.d[key] = (time.time() + ttl, payload)


class StoreCache:
    """委托给 store.cache_get_many / cache_put；任何异常都当作未命中，不影响主流程。"""
    def __init__(self, store):
        self.store = store

    def get(self, key):
        return self.get_many([key]).get(key)

    def get_many(self, keys):
        try:
            return self.store.cache_get_many(list(keys)) if keys else {}
        except Exception:  # noqa: BLE001
            return {}

    def put(self, key, provider, payload, ttl):
        try:
            self.store.cache_put(key, provider, payload, ttl)
        except Exception:  # noqa: BLE001
            pass
