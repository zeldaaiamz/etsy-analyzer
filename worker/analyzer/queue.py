"""作业队列存储抽象：SupabaseStore（PostgREST + service_role）与 MemoryStore（mock/测试）。"""
import threading
import uuid
from datetime import datetime, timezone, timedelta
from .http import request_json, request_bytes


def _now():
    return datetime.now(timezone.utc)


class Store:
    def claim_next_job(self, worker_id): ...
    def update_job(self, job_id, **fields): ...
    def save_etsy_product(self, job, data): ...
    def save_analysis(self, job, data): ...
    def save_amazon_matches(self, job, rows): ...
    def upload_image(self, path, data, content_type): ...
    def save_snapshots(self, job, rows): ...
    def is_cancel_requested(self, job_id): return False
    def get_service_status(self): return {}
    def set_service_status(self, **f): ...
    def heartbeat(self, worker_id, current_job=None, info=None): ...
    def bump_usage(self, provider, calls, units, hits=0): ...
    def cache_get_many(self, keys): return {}
    def cache_put(self, key, provider, payload, ttl): ...
    def discard_exhausted_job(self, job_id, error): ...
    def purge_expired_cache(self): return 0
    def load_etsy_product(self, job): return None
    def lookup_request(self, listing_id): return None   # 033 ehunt_lookup_requests（ETSY_PROVIDER=ehunt_web）
    def lookup_get(self, listing_id): return None


class MemoryStore(Store):
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs, self.etsy, self.analyses, self.matches = {}, {}, {}, []
        self.files, self.snapshots = {}, []
        self.status, self.heartbeats, self.usage, self.kv = {}, {}, {}, {}
        self.library = set()  # (user_id, listing_id) 或 job_id

    def discard_exhausted_job(self, job_id, error):
        with self.lock:
            j = self.jobs[job_id]
            lid = j.get("etsy_listing_id") or (self.etsy.get(job_id) or {}).get("listing_id")
            if job_id in self.library or (j["user_id"], str(lid)) in self.library:
                j.update(status="failed", error=error, finished_at=_now(), locked_by=None)
                return "failed"
            del self.jobs[job_id]
            self.etsy.pop(job_id, None); self.analyses.pop(job_id, None)
            self.matches = [m for m in self.matches if m["job_id"] != job_id]
            self.snapshots = [s for s in self.snapshots if s["job_id"] != job_id]
            return "deleted"

    def enqueue(self, url, user_id="mock-user", batch_id="mock-batch", prescreen=False):
        jid = str(uuid.uuid4())
        self.jobs[jid] = dict(id=jid, etsy_url=url, user_id=user_id, batch_id=batch_id,
                              status="queued", attempts=0, max_attempts=3, run_after=_now(),
                              created_at=_now(), queued_at=_now(), error=None,
                              prescreen=prescreen, screen_state=None)
        return jid

    def load_etsy_product(self, job):
        return self.etsy.get(job["id"])

    def screen_confirm(self, keep, reject=()):
        """与 017 screen_confirm 一致：只作用于 status='screening' 的预筛选任务。"""
        with self.lock:
            nk = nr = 0
            for jid in keep:
                j = self.jobs.get(jid)
                if j and j.get("prescreen") and j["status"] == "screening":
                    j.update(status="queued", screen_state="kept", screened_at=_now(), attempts=0, error=None,
                             stage=None, run_after=_now(), queued_at=_now(), finished_at=None); nk += 1
            for jid in reject:
                j = self.jobs.get(jid)
                if j and j.get("prescreen") and j["status"] == "screening":
                    j.update(status="canceled", screen_state="rejected", screened_at=_now(), finished_at=_now(),
                             error="筛选时已排除"); nr += 1
            return {"kept": nk, "rejected": nr}

    def claim_next_job(self, worker_id):
        with self.lock:  # 等价于 FOR UPDATE SKIP LOCKED
            pu = self.status.get("ehunt_paused_until")
            paused = bool(pu and pu > _now())
            q = sorted((j for j in self.jobs.values()
                        if j["status"] == "queued" and not j.get("cancel_requested")
                        and (j["run_after"] <= _now() or (paused and "额度" in (j.get("error") or "")))
                        and (not paused or j.get("screen_state") == "kept" or self._cached(j))),
                       key=lambda j: (0 if j.get("prescreen") and j.get("screen_state") != "kept" else 1, j["created_at"]))
            if not q:
                return None
            j = q[0]
            j.update(status="running", locked_by=worker_id, attempts=j["attempts"] + 1,
                     started_at=_now(), finished_at=None, error=None,
                     stage="analyze" if j.get("screen_state") == "kept" else "fetch_etsy")
            return dict(j)

    def job_exists(self, job_id):
        return job_id in self.jobs

    def _cached(self, j):
        """熔断中只放行 24h 缓存内的 listing（0 credit），与 claim_next_job (010) 一致。"""
        from .etsy_url import parse_listing_id
        lid = j.get("etsy_listing_id") or parse_listing_id(j["etsy_url"])
        v = self.kv.get(f"ehunt:listing:{lid}") if lid else None
        return bool(v and v[0] > _now())

    def update_job(self, job_id, **f):
        with self.lock:
            self.jobs[job_id].update(f)

    def save_etsy_product(self, job, data):
        self.etsy[job["id"]] = data

    def save_analysis(self, job, data):
        self.analyses[job["id"]] = data

    def save_amazon_matches(self, job, rows):
        self.matches.extend(dict(r, job_id=job["id"]) for r in rows)

    def upload_image(self, path, data, content_type):
        self.files[path] = (data, content_type)

    def save_snapshots(self, job, rows):
        self.snapshots.extend(dict(r, job_id=job["id"]) for r in rows)

    def is_cancel_requested(self, job_id):
        return bool(self.jobs[job_id].get("cancel_requested"))

    def get_service_status(self):
        return dict(self.status)

    def set_service_status(self, **f):
        self.status.update(f)

    def heartbeat(self, worker_id, current_job=None, info=None):
        self.heartbeats[worker_id] = dict(last_seen=_now(), current_job=current_job, info=info)

    def bump_usage(self, provider, calls, units, hits=0):
        u = self.usage.setdefault(provider, [0, 0, 0])
        u[0] += calls; u[1] += units; u[2] += hits

    def cache_get_many(self, keys):
        return {k: v[1] for k in keys if (v := self.kv.get(k)) and v[0] > _now()}

    def cache_put(self, key, provider, payload, ttl):
        self.kv[key] = (_now() + timedelta(seconds=ttl), payload)

    def purge_expired_cache(self):
        with self.lock:
            dead = [k for k, v in self.kv.items() if v[0] <= _now()]
            for k in dead:
                del self.kv[k]
            return len(dead)


class SupabaseStore(Store):
    def __init__(self, url, service_key, bucket="snapshots"):
        if not url or not service_key:
            raise ValueError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 未配置")
        self.base = url.rstrip("/") + "/rest/v1"
        self.storage = url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self.h = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}

    def _call(self, method, path, body=None, params=None, prefer="return=minimal"):
        return request_json(method, self.base + path, {**self.h, "Prefer": prefer}, params, body)

    v2 = None  # None=未探测；True/False=017 claim_next_job_v2 是否已部署

    def claim_next_job(self, worker_id):
        """优先 claim_next_job_v2（017，含预筛选任务）；未部署则回退旧版（旧版跳过预筛选任务）。"""
        if self.v2 is not False:
            try:
                rows = self._call("POST", "/rpc/claim_next_job_v2", {"p_worker": worker_id}, prefer="")
                self.v2 = True
                return rows[0] if rows else None
            except Exception as e:  # noqa: BLE001
                if self.v2 is None and ("HTTP 404" in str(e) or "PGRST202" in str(e)):
                    self.v2 = False
                else:
                    raise
        rows = self._call("POST", "/rpc/claim_next_job", {"p_worker": worker_id}, prefer="")
        return rows[0] if rows else None

    def load_etsy_product(self, job):
        rows = self._call("GET", "/etsy_products", params={"job_id": f"eq.{job['id']}", "select": "*"}, prefer="")
        return rows[0] if rows else None

    def update_job(self, job_id, **f):
        for k, v in list(f.items()):
            if isinstance(v, datetime):
                f[k] = v.isoformat()
        self._call("PATCH", "/jobs", f, {"id": f"eq.{job_id}"})

    def _upsert(self, table, body):
        self._call("POST", f"/{table}", body, prefer="resolution=merge-duplicates,return=minimal")

    def save_etsy_product(self, job, data):
        self._upsert("etsy_products", {**data, "job_id": job["id"], "user_id": job["user_id"]})

    def save_analysis(self, job, data):
        self._upsert("analyses", {**data, "job_id": job["id"], "user_id": job["user_id"]})

    def save_amazon_matches(self, job, rows):
        self._call("DELETE", "/amazon_matches", params={"job_id": f"eq.{job['id']}"})  # 重试时幂等
        if rows:
            self._call("POST", "/amazon_matches",
                       [{**r, "job_id": job["id"], "user_id": job["user_id"]} for r in rows])


    def upload_image(self, path, data, content_type):
        request_bytes("POST", f"{self.storage}/object/{self.bucket}/{path}",
                      {**self.h, "Content-Type": content_type, "x-upsert": "true"}, data)

    def save_snapshots(self, job, rows):
        self._call("DELETE", "/evidence_snapshots", params={"job_id": f"eq.{job['id']}"})
        if rows:
            keys = sorted({k for r in rows for k in r})  # PostgREST 批量插入要求每行键一致
            self._call("POST", "/evidence_snapshots",
                       [{**{k: r.get(k) for k in keys}, "job_id": job["id"], "user_id": job["user_id"]} for r in rows])

    def job_exists(self, job_id):
        return bool(self._call("GET", "/jobs", params={"id": f"eq.{job_id}", "select": "id"}, prefer=""))

    def is_cancel_requested(self, job_id):
        rows = self._call("GET", "/jobs", params={"id": f"eq.{job_id}", "select": "cancel_requested,status"}, prefer="")
        return bool(rows and (rows[0].get("cancel_requested") or rows[0].get("status") == "canceled"))

    def get_service_status(self):
        rows = self._call("GET", "/service_status", params={"id": "eq.1", "select": "*"}, prefer="")
        return rows[0] if rows else {}

    def set_service_status(self, **f):
        f = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in f.items()}
        self._call("PATCH", "/service_status", {**f, "updated_at": _now().isoformat()}, {"id": "eq.1"})

    def heartbeat(self, worker_id, current_job=None, info=None):
        self._upsert("worker_heartbeat", {"worker_id": worker_id, "last_seen": _now().isoformat(),
                                          "current_job": current_job, "info": info or {}})

    def bump_usage(self, provider, calls, units, hits=0):
        self._call("POST", "/rpc/bump_usage", {"p_provider": provider, "p_calls": calls, "p_units": units, "p_hits": hits})

    def cache_get_many(self, keys):
        out = {}
        for i in range(0, len(keys), 50):
            part = keys[i:i + 50]
            rows = self._call("GET", "/provider_cache", params={
                "select": "key,payload", "key": "in.(" + ",".join('"' + k.replace('"', '') + '"' for k in part) + ")",
                "expires_at": f"gt.{_now().isoformat()}"}, prefer="") or []
            out.update({r["key"]: r["payload"] for r in rows})
        return out

    def cache_put(self, key, provider, payload, ttl):
        self._upsert("provider_cache", {"key": key, "provider": provider, "payload": payload,
                                        "created_at": _now().isoformat(),
                                        "expires_at": (_now() + timedelta(seconds=ttl)).isoformat()})


    def lookup_request(self, listing_id):
        """033 ehunt_lookup_request：幂等写入/重新排队，返回当前行。"""
        r = self._call("POST", "/rpc/ehunt_lookup_request", {"p_listing": int(listing_id)}, prefer="")
        return (r[0] if isinstance(r, list) else r) or None

    def lookup_get(self, listing_id):
        rows = self._call("GET", "/ehunt_lookup_requests", params={"listing_id": f"eq.{int(listing_id)}", "select": "*"}, prefer="")
        return rows[0] if rows else None

    def purge_expired_cache(self):
        """L5：删除过期超过 1 小时的 provider_cache 行（015 purge_expired_provider_cache；未部署时静默跳过）。"""
        return self._call("POST", "/rpc/purge_expired_provider_cache", {}, prefer="") or 0

    def discard_exhausted_job(self, job_id, error):
        r = self._call("POST", "/rpc/discard_exhausted_job", {"p_job": job_id, "p_error": error}, prefer="") or {}
        paths = r.get("paths") or []
        if paths:
            try:
                request_json("DELETE", f"{self.storage}/object/{self.bucket}", self.h, None, {"prefixes": paths})
            except Exception:  # noqa: BLE001  截图清理失败不影响任务删除
                pass
        return r.get("action")


def retry_delay(attempts):
    return timedelta(seconds=min(600, 30 * 2 ** (attempts - 1)))


def next_utc_midnight(now=None):
    n = now or _now()
    return (n + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
