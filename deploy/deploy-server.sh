#!/usr/bin/env bash
# 把已推送到 GitHub main 的 worker 代码部署到阿里云 ECS（/opt/etsy-analyzer，systemd: etsy-analyzer-worker）。
# 用法：deploy/deploy-server.sh            # 拉取 origin/main，requirements 变化时重装依赖，重启服务
#       deploy/deploy-server.sh --status   # 只看服务状态与最近日志
# 前提：改动已 push（auto-sync 会先 push 再调用本脚本）。服务器上的 worker/.env 不在 git 中，不会被覆盖。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ECS_PY:-/workspace/.sshvenv/bin/python}"
WANT="$(git -C "$ROOT" rev-parse origin/main)"
if [ "${1:-}" = "--status" ]; then
  echo 'systemctl --no-pager status etsy-analyzer-worker | head -12; journalctl -u etsy-analyzer-worker -n 20 --no-pager' | "$PY" "$ROOT/deploy/server_exec.py"; exit
fi
"$PY" "$ROOT/deploy/server_exec.py" <<REMOTE
set -e
cd /opt/etsy-analyzer
OLD_REQ=\$(sha1sum worker/requirements.txt | cut -c1-40)
git fetch -q origin main && git reset -q --hard $WANT
[ "\$(sha1sum worker/requirements.txt | cut -c1-40)" = "\$OLD_REQ" ] || .venv/bin/pip install -q -r worker/requirements.txt
systemctl restart etsy-analyzer-worker
sleep 3; echo "deployed \$(git rev-parse --short HEAD) → \$(systemctl is-active etsy-analyzer-worker)"
REMOTE
