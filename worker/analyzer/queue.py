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


class MemoryStore(Store):
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs, self.etsy, self.analyses, self.matches = {}, {}, {}, []
        self.files, self.snapshots = {}, []
        self.status, self.heartbeats, self.usage, self.kv = {}, {}, {}, {}

    def enqueue(self, url, user_id="mock-user", batch_id="mock-batch"):
        jid = str(uuid.uuid4())
        self.jobs[jid] = dict(id=jid, etsy_url=url, user_id=user_id, batch_id=batch_id,
                              status="queued", attempts=0, max_attempts=3, run_after=_now(),
                              created_at=_now(), queued_at=_now(), error=None)
        return jid

    def claim_next_job(self, worker_id):
        with self.lock:  # 等价于 FOR UPDATE SKIP LOCKED
            pu = self.status.get("ehunt_paused_until")
            paused = bool(pu and pu > _now())
            q = sorted((j for j in self.jobs.values()
                        if j["status"] == "queued" and not j.get("cancel_requested")
                        and (j["run_after"] <= _now() or (paused and "额度" in (j.get("error") or "")))
                        and (not paused or self._cached(j))),
                       key=lambda j: j["created_at"])
            if not q:
                return None
            j = q[0]
            j.update(status="running", locked_by=worker_id, attempts=j["attempts"] + 1,
                     started_at=_now(), finished_at=None, stage="fetch_etsy", error=None)
            return dict(j)

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

    def claim_next_job(self, worker_id):
        rows = self._call("POST", "/rpc/claim_next_job", {"p_worker": worker_id}, prefer="")
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


def retry_delay(attempts):
    return timedelta(seconds=min(600, 30 * 2 ** (attempts - 1)))


def next_utc_midnight(now=None):
    n = now or _now()
    return (n + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
