#!/usr/bin/env bash
# 选品抓取 worker —— 运行在本 box（不在 ECS；原 etsy-selection-worker.service 设计已作废并删除）。
# 用法：deploy/box-selection-worker.sh login | start | ensure | watch | stop | status | once-fake <list.json>（start = 带自动重启的守护循环）
#       reviews-start | reviews-stop | reviews-status | reviews-dry <listing_id…>  —— 049 产品库评论日期自动抓取（独立进程；评论优先，不因 EHunt 忙让路；选品 worker nice 10）
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
         # 单实例锁（同 reviews-start）：先拿 $DATA/worker.lock 再启动，锁 fd 7 由守护循环继承持有 → 第二次 start 拿不到锁直接退出，不会两个 supervisor 抢同一个 ehunt-profile
         exec 7>"$DATA/worker.lock"; flock -n 7 || { echo "already running (lock $DATA/worker.lock held)"; exit 0; }
         nohup "$SELF" _supervise >>"$LOG" 2>&1 & echo $! >"$PID"; echo "started $(cat "$PID") (supervised), log $LOG" ;;
  _supervise)
         # 兜底：直接调用 _supervise（不经 start）也必须拿到锁；经 start 时 fd 7 已继承（同一打开文件，flock 直接成功）
         { true >&7; } 2>/dev/null || exec 7>"$DATA/worker.lock"
         flock -n 7 || { echo "$(date '+%F %T') SUPERVISOR lock $DATA/worker.lock held by another supervisor; exiting"; exit 0; }
         child=""; trap '[ -n "$child" ] && kill -TERM "$child" 2>/dev/null; wait "$child" 2>/dev/null; exit 0' TERM INT
         delay=5
         while true; do
           t0=$(date +%s); nice -n 10 python3 -m selection.ehunt_worker & child=$!; set +e; wait "$child"; code=$?; set -e; child=""
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
         [ -f "$ROOT/deploy/.selection-disabled" ] && exit 0   # 2026-10-30：watch 的 ensure 之前不看开关，stop 后 5 分钟内又被拉起
         if ! flock -n "$DATA/worker.lock" true; then exit 0; fi   # 锁被持有 = 有 supervisor 在跑
         if [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then exit 0; fi
         echo "$(date '+%F %T') ENSURE worker not running (stale pid $(cat "$PID" 2>/dev/null || echo -)); starting" >>"$LOG"; exec "$SELF" start ;;
  watch) # 周期自检（box 无 cron/systemd）：每 WATCH_SECONDS（默认 300）秒对选品 worker 与评论 worker 各执行一次幂等 ensure；flock 保证只有一个 watch
         exec 9>"$DATA/watch.lock"; flock -n 9 || exit 0
         while true; do
           "$SELF" ensure >/dev/null 2>&1 || true
           [ ! -f "$ROOT/deploy/.reviews-disabled" ] && { "$SELF" reviews-start >/dev/null 2>&1 || true; }
           sleep "${WATCH_SECONDS:-300}"
         done ;;
  status) if ! flock -n "$DATA/worker.lock" true; then echo "running ($(cat "$PID" 2>/dev/null || echo ?), lock held)"
          elif [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then echo "running ($(cat "$PID"), no lock)"; else echo "not running"; fi; tail -n 5 "$LOG" 2>/dev/null || true ;;
  reviews-start) load_env; RP="$DATA/reviews.pid"; [ -f "$RP" ] && kill -0 "$(cat "$RP")" 2>/dev/null && { echo "reviews already running ($(cat "$RP"))"; exit 0; }
         # 单实例锁：先拿 flock 再启动，锁 fd 由守护循环/python 继承持有 → 并发的第二次 start（ensure-worker.sh 与 watch 竞态）拿不到锁直接退出，不会出现两个 worker 抢同一个浏览器 profile
         exec 8>"$DATA/reviews.lock"; flock -n 8 || { echo "reviews already running (lock $DATA/reviews.lock held)"; exit 0; }
         setsid nohup bash -c 'while true; do python3 -m selection.review_scraper; echo "$(date "+%F %T") reviews worker exited $?; restart in 30s"; sleep 30; done' >>"$DATA/logs/reviews.log" 2>&1 &
         echo $! >"$RP"; echo "reviews started $(cat "$RP"), log $DATA/logs/reviews.log" ;;
  # setsid → pid = 进程组，reviews-stop 连同 python/浏览器一起停
  reviews-stop) RP="$DATA/reviews.pid"; if [ -f "$RP" ] && kill -- "-$(cat "$RP")" 2>/dev/null; then rm -f "$RP"; echo stopped; else echo "not running"; fi ;;
  reviews-status) RP="$DATA/reviews.pid"; [ -f "$RP" ] && kill -0 "$(cat "$RP")" 2>/dev/null && echo "reviews running ($(cat "$RP"))" || echo "reviews not running"; tail -n 5 "$DATA/logs/reviews.log" 2>/dev/null || true ;;
  reviews-dry) load_env; shift; exec python3 -m selection.review_scraper --dry-run "$@" ;;
  once-fake) load_env; exec python3 -m selection.ehunt_worker --once --fake "$2" ;;
  *) sed -n 2,6p "$0"; exit 1 ;;
esac
