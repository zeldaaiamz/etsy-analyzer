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


class MemoryStore(Store):
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs, self.etsy, self.analyses, self.matches = {}, {}, {}, []
        self.files, self.snapshots = {}, []

    def enqueue(self, url, user_id="mock-user", batch_id="mock-batch"):
        jid = str(uuid.uuid4())
        self.jobs[jid] = dict(id=jid, etsy_url=url, user_id=user_id, batch_id=batch_id,
                              status="queued", attempts=0, max_attempts=3, run_after=_now(),
                              created_at=_now(), error=None)
        return jid

    def claim_next_job(self, worker_id):
        with self.lock:  # 等价于 FOR UPDATE SKIP LOCKED
            q = sorted((j for j in self.jobs.values()
                        if j["status"] == "queued" and j["run_after"] <= _now()),
                       key=lambda j: j["created_at"])
            if not q:
                return None
            j = q[0]
            j.update(status="running", locked_by=worker_id, attempts=j["attempts"] + 1)
            return dict(j)

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


def retry_delay(attempts):
    return timedelta(seconds=min(600, 30 * 2 ** (attempts - 1)))
