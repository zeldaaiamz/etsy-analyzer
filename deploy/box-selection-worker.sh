#!/usr/bin/env bash
# 选品抓取 worker —— 运行在本 box（不在 ECS；原 etsy-selection-worker.service 设计已作废并删除）。
# 用法：deploy/box-selection-worker.sh login | start | stop | status | once-fake <list.json>
# 环境：从 SEL_ENV_FILE（默认 ~/.config/etsy-selection/worker.env，权限 600）读取 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / ARK_API_KEY；本脚本不打印任何密钥。
# 浏览器：自己的 Playwright 持久化目录 EHUNT_PROFILE_DIR（默认 ~/.local/share/etsy-selection/ehunt-profile），不使用桌面浏览器的 cookie/profile。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENVF="${SEL_ENV_FILE:-$HOME/.config/etsy-selection/worker.env}"
DATA="$HOME/.local/share/etsy-selection"; mkdir -p "$DATA/logs"; PID="$DATA/worker.pid"; LOG="$DATA/logs/worker.log"
load_env() { [ -f "$ENVF" ] && { set -a; . "$ENVF"; set +a; } || true; }
cd "$ROOT/worker"
case "${1:-}" in
  login) export DISPLAY="${DISPLAY:-:1}"; exec python3 -m selection.ehunt_worker --login ;;
  start) load_env; [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null && { echo "already running ($(cat "$PID"))"; exit 0; }
         nohup python3 -m selection.ehunt_worker >>"$LOG" 2>&1 & echo $! >"$PID"; echo "started $(cat "$PID"), log $LOG" ;;
  stop) [ -f "$PID" ] && kill "$(cat "$PID")" 2>/dev/null && rm -f "$PID" && echo stopped || echo "not running" ;;
  status) [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null && echo "running ($(cat "$PID"))" || echo "not running"; tail -n 5 "$LOG" 2>/dev/null || true ;;
  once-fake) load_env; exec python3 -m selection.ehunt_worker --once --fake "$2" ;;
  *) sed -n 2,6p "$0"; exit 1 ;;
esac
