#!/usr/bin/env bash
# 幂等：若 worker 未运行则在后台启动（已运行时 flock 直接退出）。
# 由 /etc/profile.d/etsy-analyzer-worker.sh 和 ~/.bashrc 调用。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$ROOT/worker/.env" ] || exit 0
setsid nohup "$ROOT/deploy/run-worker.sh" >/dev/null 2>&1 < /dev/null &
