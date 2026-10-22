#!/usr/bin/env bash
# 幂等：若 worker / auto-sync 未运行则在后台启动（已运行时 flock 直接退出）。
# 由 /etc/profile.d/etsy-analyzer-worker.sh 和 ~/.bashrc 调用。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 自动同步：提交/推送 web、worker 改动，并在 worker 代码变化时重启 worker
setsid nohup "$ROOT/deploy/auto-sync.sh" >/dev/null 2>&1 < /dev/null &
# 049 评论日期自动抓取（box 独立进程，幂等：已在跑则什么都不做；自带重启循环 + 专用 Xvfb :77）
[ -f "${SEL_ENV_FILE:-$HOME/.config/etsy-selection/worker.env}" ] && [ ! -f "$ROOT/deploy/.reviews-disabled" ] && "$ROOT/deploy/box-selection-worker.sh" reviews-start >/dev/null 2>&1 < /dev/null
# 选品/预筛选取数 worker（box 重启/休眠后守护进程会消失）：幂等 ensure + 后台 watch 每 5 分钟自检（选品 + 评论）；.selection-disabled 可关闭
if [ -f "${SEL_ENV_FILE:-$HOME/.config/etsy-selection/worker.env}" ] && [ ! -f "$ROOT/deploy/.selection-disabled" ]; then
  "$ROOT/deploy/box-selection-worker.sh" ensure >/dev/null 2>&1 < /dev/null
  setsid nohup "$ROOT/deploy/box-selection-worker.sh" watch >/dev/null 2>&1 < /dev/null &
fi
[ -f "$ROOT/worker/.env" ] || exit 0
# worker 已迁到阿里云 ECS（systemd: etsy-analyzer-worker）；存在该标记时 box 上不再启动 worker，避免两个 worker 抢队列
[ -f "$ROOT/deploy/.worker-disabled-on-box" ] && exit 0
setsid nohup "$ROOT/deploy/run-worker.sh" >/dev/null 2>&1 < /dev/null &
