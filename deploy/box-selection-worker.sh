#!/usr/bin/env bash
# 选品抓取 worker —— 运行在本 box（不在 ECS；原 etsy-selection-worker.service 设计已作废并删除）。
# 用法：deploy/box-selection-worker.sh login | start | ensure | stop | status | once-fake <list.json>（start = 带自动重启的守护循环）
#       reviews-start | reviews-stop | reviews-status | reviews-dry <listing_id…>  —— 049 产品库评论日期自动抓取（独立进程，nice 10，不影响 EHunt 队列）
# 环境：从 SEL_ENV_FILE（默认 ~/.config/etsy-selection/worker.env，权限 600）读取 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / ARK_API_KEY；本脚本不打印任何密钥。
# 浏览器：自己的 Playwright 持久化目录 EHUNT_PROFILE_DIR（默认 ~/.local/share/etsy-selection/ehunt-profile），不使用桌面浏览器的 cookie/profile。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; SELF="$ROOT/deploy/$(basename "$0")"
ENVF="${SEL_ENV_FILE:-$HOME/.config/etsy-selection/worker.env}"
DATA="$HOME/.local/share/etsy-selection"; mkdir -p "$DATA/logs"; PID="$DATA/worker.pid"; LOG="$DATA/logs/worker.log"
load_env() { [ -f "$ENVF" ] && { set -a; . "$ENVF"; set +a; } || true; }
cd "$ROOT/worker"
case "${1:-}" in
  login) export DISPLAY="${DISPLAY:-:1}"; exec python3 -m selection.ehunt_worker --login ;;
  start) load_env; [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null && { echo "already running ($(cat "$PID"))"; exit 0; }
         # 守护循环：worker 任何原因退出（含 Playwright 自愈退出码 75、崩溃）都自动重启；连续快速退出时退避（5s→最多 300s）
         nohup "$SELF" _supervise >>"$LOG" 2>&1 & echo $! >"$PID"; echo "started $(cat "$PID") (supervised), log $LOG" ;;
  _supervise)
         child=""; trap '[ -n "$child" ] && kill -TERM "$child" 2>/dev/null; wait "$child" 2>/dev/null; exit 0' TERM INT
         delay=5
         while true; do
           t0=$(date +%s); python3 -m selection.ehunt_worker & child=$!; set +e; wait "$child"; code=$?; set -e; child=""
           ran=$(( $(date +%s) - t0 ))
           if [ "$ran" -ge 120 ]; then delay=5; else delay=$(( delay * 2 > 300 ? 300 : delay * 2 )); fi
           echo "$(date '+%F %T') SUPERVISOR worker exited code=$code after ${ran}s; restarting in ${delay}s"
           sleep "$delay" & wait $! || true
         done ;;
  stop) if [ -f "$PID" ] && kill "$(cat "$PID")" 2>/dev/null; then
          p="$(cat "$PID")"; for _ in $(seq 1 40); do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done   # 等 worker 关闭浏览器（profile 锁）
          rm -f "$PID"; echo stopped
        else echo "not running"; fi ;;
  ensure) # box 休眠/重启后守护进程会消失（2026-10-22 16:34 重启后 worker.pid 指向已不存在的进程）：没在运行才启动，已在运行什么都不做（不重启、不打断正在跑的批次）
         if [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then exit 0; fi
         echo "$(date '+%F %T') ENSURE worker not running (stale pid $(cat "$PID" 2>/dev/null || echo -)); starting" >>"$LOG"; exec "$SELF" start ;;
  status) [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null && echo "running ($(cat "$PID"))" || echo "not running"; tail -n 5 "$LOG" 2>/dev/null || true ;;
  reviews-start) load_env; RP="$DATA/reviews.pid"; [ -f "$RP" ] && kill -0 "$(cat "$RP")" 2>/dev/null && { echo "reviews already running ($(cat "$RP"))"; exit 0; }
         setsid nohup bash -c 'while true; do nice -n 10 python3 -m selection.review_scraper; echo "$(date "+%F %T") reviews worker exited $?; restart in 30s"; sleep 30; done' >>"$DATA/logs/reviews.log" 2>&1 &
         echo $! >"$RP"; echo "reviews started $(cat "$RP"), log $DATA/logs/reviews.log" ;;
  # setsid → pid = 进程组，reviews-stop 连同 python/浏览器一起停
  reviews-stop) RP="$DATA/reviews.pid"; if [ -f "$RP" ] && kill -- "-$(cat "$RP")" 2>/dev/null; then rm -f "$RP"; echo stopped; else echo "not running"; fi ;;
  reviews-status) RP="$DATA/reviews.pid"; [ -f "$RP" ] && kill -0 "$(cat "$RP")" 2>/dev/null && echo "reviews running ($(cat "$RP"))" || echo "reviews not running"; tail -n 5 "$DATA/logs/reviews.log" 2>/dev/null || true ;;
  reviews-dry) load_env; shift; exec python3 -m selection.review_scraper --dry-run "$@" ;;
  once-fake) load_env; exec python3 -m selection.ehunt_worker --once --fake "$2" ;;
  *) sed -n 2,6p "$0"; exit 1 ;;
esac
