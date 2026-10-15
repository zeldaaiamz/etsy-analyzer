#!/usr/bin/env bash
# 自动同步：每 60 秒检查 web/ worker/ supabase/ deploy/ tools/ 的改动 → 提交并推送（推送 web/ 触发 GitHub Pages 部署）；
# worker/ 代码有变化时：worker 已迁到阿里云 ECS（存在 deploy/.worker-disabled-on-box）→ 调 deploy/deploy-server.sh 部署并重启服务；否则重启本机 worker。
# 安全：只 add 白名单路径；.env/日志/截图由 .gitignore 排除，另外提交前再扫一遍敏感文件名与密钥特征，命中则中止本轮。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# 暂停开关：存在 deploy/.auto-sync-paused 时不启动、不提交
[ -f "$ROOT/deploy/.auto-sync-paused" ] && exit 0
LOG="$ROOT/logs/auto-sync.log"; mkdir -p "$ROOT/logs"
exec 8>"$ROOT/logs/auto-sync.lock"
flock -n 8 || { echo "auto-sync already running"; exit 0; }
echo $$ > "$ROOT/logs/auto-sync.pid"
PATHS=(web worker supabase deploy tools)
INTERVAL="${AUTO_SYNC_INTERVAL:-60}"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
G() { git -C "$ROOT" -c user.name=zeldaaiamz -c user.email=zeldaaiamz@users.noreply.github.com "$@"; }
worker_hash() { find "$ROOT/worker/analyzer" "$ROOT/worker/requirements.txt" -type f \( -name '*.py' -o -name '*.txt' \) -print0 2>/dev/null | sort -z | xargs -0 sha1sum | sha1sum | cut -c1-40; }
LAST_WH="$(worker_hash)"

cycle() {
  [ -f "$ROOT/deploy/.auto-sync-paused" ] && { log "paused"; return; }
  local branch; branch="$(G rev-parse --abbrev-ref HEAD)"
  [ "$branch" = "main" ] || { log "skip: on branch $branch"; return; }
  if [ -n "$(G status --porcelain -- "${PATHS[@]}")" ]; then
    G add -A -- "${PATHS[@]}"
    local staged; staged="$(G diff --cached --name-only)"
    if echo "$staged" | grep -Eiq '(^|/)\.env($|\.)|\.pem$|\.key$|(^|/)secrets/|(^|/)logs/|screenshots/|\.log$|id_rsa'; then
      log "ABORT: sensitive file staged: $(echo "$staged" | grep -Ei '\.env|\.pem|\.key|secrets/|logs/|screenshots/|\.log$|id_rsa' | tr '\n' ' ')"; G reset -q; return
    fi
    if G diff --cached -U0 | grep -E '^\+' | grep -Eq 'service_role"|SERVICE_ROLE_KEY=.{20,}|sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY'; then
      log "ABORT: secret-looking content in diff"; G reset -q; return
    fi
    if [ -n "$staged" ]; then
      G commit -q -m "auto-sync: $(echo "$staged" | head -5 | tr '\n' ' ')$( [ "$(echo "$staged" | wc -l)" -gt 5 ] && echo '…')" && log "committed: $(echo "$staged" | tr '\n' ' ')"
    fi
  fi
  if [ -n "$(G log --oneline origin/main..HEAD 2>/dev/null)" ]; then
    if G push -q origin main >> "$LOG" 2>&1; then log "pushed $(G rev-parse --short HEAD)"
    else log "push failed; try pull --rebase"; G pull -q --rebase origin main >> "$LOG" 2>&1 && G push -q origin main >> "$LOG" 2>&1 && log "pushed after rebase" || { G rebase --abort 2>/dev/null; log "push still failing"; }; fi
  fi
  local wh; wh="$(worker_hash)"
  if [ "$wh" != "$LAST_WH" ]; then
    LAST_WH="$wh"
    if [ -f "$ROOT/deploy/.worker-disabled-on-box" ]; then
      # worker 跑在阿里云 ECS：确认已推送后部署到服务器（失败则下轮重试）
      if [ -z "$(G log --oneline origin/main..HEAD 2>/dev/null)" ] && "$ROOT/deploy/deploy-server.sh" >> "$LOG" 2>&1; then log "worker code changed → deployed to ECS"
      else LAST_WH=""; log "worker code changed but ECS deploy failed/unpushed; will retry"; fi
    elif pkill -f 'python3 -m analyzer.main'; then log "worker code changed → worker restarted"; else log "worker code changed (worker not running)"; fi
  fi
}
log "auto-sync started pid $$ interval ${INTERVAL}s"
while true; do
  ( cd "$ROOT" && G fetch -q origin main 2>/dev/null ); cycle 2>> "$LOG"
  sleep "$INTERVAL"
done
