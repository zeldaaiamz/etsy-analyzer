#!/usr/bin/env bash
# 把已推送到 GitHub main 的 worker 代码部署到阿里云 ECS（/opt/etsy-analyzer，systemd: etsy-analyzer-worker）。
# 用法：deploy/deploy-server.sh            # 部署 origin/main（ECS 先直连 GitHub fetch，失败则由 box 推 git bundle），requirements 变化时重装依赖，重启服务
#       deploy/deploy-server.sh --status   # 只看服务状态与最近日志
# 原则：代码没有确认更新到目标提交（HEAD == WANT）就退出、不重启服务；重启前失败回滚到原 HEAD；worker 忙时等待（IDLE_MAX_SECONDS，默认 1800）。
# 前提：改动已 push（auto-sync 会先 push 再调用本脚本）。服务器上的 worker/.env 不在 git 中，不会被覆盖。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ECS_PY:-/workspace/.sshvenv/bin/python}"
if [ "${1:-}" = "--status" ]; then
  echo 'systemctl --no-pager status etsy-analyzer-worker | head -12; journalctl -u etsy-analyzer-worker -n 20 --no-pager' | "$PY" "$ROOT/deploy/server_exec.py"; exit
fi
# 1) box 侧先拿到 GitHub 最新 main（box 能连 GitHub）；失败直接退出
timeout 60 git -C "$ROOT" fetch -q origin main || { echo "box: git fetch origin main 失败，未部署" >&2; exit 1; }
WANT="$(git -C "$ROOT" rev-parse origin/main)"
# 2) ECS 当前 HEAD
CUR="$(echo 'git -C /opt/etsy-analyzer rev-parse HEAD' | "$PY" "$ROOT/deploy/server_exec.py")" || { echo "ECS: 无法读取 HEAD，未部署" >&2; exit 1; }
[ "$CUR" = "$WANT" ] && { echo "ECS 已是 ${WANT:0:7}，无需部署（不重启）"; exit 0; }
# 3) ECS 直连 fetch（带超时）；失败则由 box 生成增量 bundle 经 SFTP 推过去
REMOTE_BUNDLE=""
if ! echo "cd /opt/etsy-analyzer && GIT_TERMINAL_PROMPT=0 timeout 90 git fetch -q origin main && git cat-file -e $WANT^{commit}" \
     | "$PY" "$ROOT/deploy/server_exec.py"; then
  echo "ECS 直连 GitHub fetch 失败/超时 → 改用 git bundle" >&2
  B="$(mktemp -d)/etsy-analyzer.bundle"
  if git -C "$ROOT" cat-file -e "$CUR^{commit}" 2>/dev/null; then
    git -C "$ROOT" bundle create "$B" "$CUR..origin/main"
  else
    git -C "$ROOT" bundle create "$B" origin/main   # ECS HEAD 不在本地历史里 → 全量
  fi
  REMOTE_BUNDLE="/tmp/etsy-analyzer-deploy-${WANT:0:12}.bundle"
  "$PY" "$ROOT/deploy/put_bundle.py" "$B" "$REMOTE_BUNDLE" || { echo "bundle 上传失败，未部署" >&2; exit 1; }
  echo "cd /opt/etsy-analyzer && git bundle verify -q $REMOTE_BUNDLE && git fetch -q $REMOTE_BUNDLE refs/remotes/origin/main:refs/remotes/origin/main && git cat-file -e $WANT^{commit}" \
    | "$PY" "$ROOT/deploy/server_exec.py" || { echo "ECS: bundle 校验/导入失败，未部署" >&2; exit 1; }
fi
# 4) 在 ECS 上：等 worker 空闲 → 更新代码 → pip/语法/导入检查 → 重启；重启前任何一步失败都回滚到原 HEAD、不重启
{ printf 'WANT=%q\nIDLE_MAX_SECONDS=%q\nREMOTE_BUNDLE=%q\n' "$WANT" "${IDLE_MAX_SECONDS:-1800}" "$REMOTE_BUNDLE"
  cat "$ROOT/deploy/remote-deploy.sh"; } | "$PY" "$ROOT/deploy/server_exec.py"
