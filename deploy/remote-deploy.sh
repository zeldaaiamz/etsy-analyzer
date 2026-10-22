# 由 deploy-server.sh 通过 server_exec.py 在 ECS 上执行（前面会注入 WANT=、IDLE_MAX_SECONDS=、REMOTE_BUNDLE=）。
# 任何重启前步骤失败 → trap 把 /opt/etsy-analyzer 回滚到原 HEAD 并退出，不重启服务。
set -euo pipefail
cd /opt/etsy-analyzer
PREV="$(git rev-parse HEAD)"
RESTARTED=0
rollback() {
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$RESTARTED" = 0 ] && [ "$(git rev-parse HEAD)" != "$PREV" ]; then
    git reset -q --hard "$PREV" && echo "已回滚到 ${PREV:0:7}（未重启服务）" >&2
    # 依赖若已按新 requirements 装过，旧 requirements 再装一遍，保证旧进程/下次启动一致
    [ "${REQ_CHANGED:-0}" = 1 ] && .venv/bin/pip install -q -r worker/requirements.txt >&2 || true
  fi
  [ -n "${REMOTE_BUNDLE:-}" ] && rm -f "$REMOTE_BUNDLE"
  exit "$rc"
}
trap rollback EXIT

# 1) 等 analyzer worker 空闲：jobs 里没有 running 且 locked_by 属于本机 worker（WORKER_ID 及 -2、-3… 线程），
#    且 worker_heartbeat 中这些线程 current_job 均为空、心跳 2 分钟内新鲜。豆包视觉/截图都在同一个 job 的 pipeline 内，由此覆盖。
idle_check() {
  .venv/bin/python - <<'PY'
import json, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
env = {}
for l in open("worker/.env"):
    l = l.strip()
    if "=" in l and not l.startswith("#"):
        k, v = l.split("=", 1); env[k.strip()] = v.strip().strip("\"'")
url, key, wid = env["SUPABASE_URL"].rstrip("/"), env["SUPABASE_SERVICE_ROLE_KEY"], env.get("WORKER_ID", "worker-1")
H = {"apikey": key, "Authorization": "Bearer " + key}
def get(path):
    return json.load(urllib.request.urlopen(urllib.request.Request(url + "/rest/v1/" + path, headers=H), timeout=20))
like = urllib.parse.quote(wid + "*")
running = get(f"jobs?select=id,locked_by,stage&status=eq.running&locked_by=like.{like}")
hb = get(f"worker_heartbeat?select=worker_id,current_job,last_seen&worker_id=like.{like}")
now = datetime.now(timezone.utc)
busy_hb = [h for h in hb if h.get("current_job")]
stale = [h["worker_id"] for h in hb if now - datetime.fromisoformat(h["last_seen"].replace("Z", "+00:00")) > timedelta(minutes=2)]
print(f"running={len(running)} heartbeat_busy={len(busy_hb)} stale_heartbeat={stale}", file=sys.stderr)
sys.exit(0 if not running and not busy_hb else 3)
PY
}
waited=0
until idle_check; do
  [ "$waited" -ge "${IDLE_MAX_SECONDS:-1800}" ] && { echo "worker ${waited}s 内一直不空闲，放弃部署" >&2; exit 4; }
  sleep 20; waited=$((waited + 20))
done

# 2) 更新代码 + 重启前检查（失败即由 trap 回滚）
OLD_REQ="$(sha1sum worker/requirements.txt | cut -c1-40)"
git reset -q --hard "$WANT"
[ "$(git rev-parse HEAD)" = "$WANT" ] || { echo "HEAD 不是 $WANT" >&2; exit 1; }
REQ_CHANGED=0
if [ "$(sha1sum worker/requirements.txt | cut -c1-40)" != "$OLD_REQ" ]; then
  REQ_CHANGED=1; .venv/bin/pip install -q -r worker/requirements.txt
fi
.venv/bin/python -m compileall -q worker/analyzer worker/selection >/dev/null
(cd worker && ../.venv/bin/python -c "import analyzer.main") 

# 3) 重启前最后再确认一次空闲（缩小检查到重启之间的窗口）
idle_check || { echo "重启前 worker 又领到任务，回滚" >&2; exit 4; }
RESTARTED=1
systemctl restart etsy-analyzer-worker
sleep 3; echo "deployed $(git rev-parse --short HEAD) → $(systemctl is-active etsy-analyzer-worker)"
