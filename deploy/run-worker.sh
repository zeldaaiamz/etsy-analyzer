#!/usr/bin/env bash
# 无 systemd 环境（容器，PID 1=tini）下的常驻方案：
#   - flock 保证全机只有一个 worker
#   - 崩溃后 10 秒自动重启
#   - 由 ensure-worker.sh 在每次开 shell 时（含重启后第一次）拉起
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/logs/worker.log"; mkdir -p "$ROOT/logs"
exec 9>"$ROOT/logs/worker.lock"
flock -n 9 || { echo "worker already running"; exit 0; }
echo $$ > "$ROOT/logs/worker.pid"
cd "$ROOT/worker"
while true; do
  set -a; . ./.env; set +a
  echo "$(date '+%F %T') [supervisor] starting worker" >> "$LOG"
  python3 -m analyzer.main >> "$LOG" 2>&1
  echo "$(date '+%F %T') [supervisor] worker exited code=$?, restart in 10s" >> "$LOG"
  sleep 10
done
