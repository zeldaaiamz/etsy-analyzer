# etsy-analyzer

Etsy 商品链接 → 亚马逊美国站竞品/机会分析。架构详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 目录
```
supabase/migrations/001_init.sql   数据库表、RLS、队列函数
supabase/migrations/002_cancel_job_invoker.sql  安全修复：cancel_job 改 INVOKER + 列级权限
worker/                            Python worker（轮询 Supabase）
  analyzer/providers/              Etsy / Amazon / LLM 可插拔实现（含 mock）
  tests/                           单元 + mock 端到端测试
web/                               GitHub Pages 静态前端
deploy/etsy-analyzer-worker.service  systemd 单元
.github/workflows/deploy-pages.yml   前端自动部署
```

## 本地 mock 运行（无需任何 key）
```bash
cd worker
python3 -m pytest -q tests          # 3 个测试
python3 -m analyzer.main --demo https://www.etsy.com/listing/1234567890/x
```

## 上线步骤
1. 创建 Supabase 项目 → SQL Editor 依次执行 `001_init.sql`、`002_cancel_job_invoker.sql`（项目 oiymgpargbesvcfxyksj 已应用）；Auth 启用邮箱登录，Site URL / Redirect URL 填 GitHub Pages 地址，建议关闭公开注册并手动邀请自己。
2. GitHub 仓库 Settings → Pages → Source 选 “GitHub Actions”；Settings → Variables 添加 `SUPABASE_URL`、`SUPABASE_ANON_KEY`。推送 main 即部署。
3. 本机 worker：
   ```bash
   sudo mkdir -p /opt/etsy-analyzer && sudo chown box /opt/etsy-analyzer
   cp -r worker /opt/etsy-analyzer/ && python3 -m venv /opt/etsy-analyzer/venv
   /opt/etsy-analyzer/venv/bin/pip install -r /opt/etsy-analyzer/worker/requirements.txt
   cp worker/.env.example /opt/etsy-analyzer/worker/.env && chmod 600 /opt/etsy-analyzer/worker/.env
   # 编辑 .env：WORKER_MODE=real（使用 Supabase 队列），填 SUPABASE_SERVICE_ROLE_KEY；
   # *_PROVIDER 可先保持 mock（真实队列 + 假数据），拿到 key 后再逐个切换
   sudo cp deploy/etsy-analyzer-worker.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now etsy-analyzer-worker
   journalctl -u etsy-analyzer-worker -f
   ```

## 当前 box（容器，无 systemd）的常驻方式
本机 PID 1 是 tini，没有 systemd/cron，因此使用：
- `deploy/run-worker.sh`：flock 单实例 + 崩溃 10 秒后自动重启，日志 `logs/worker.log`，PID 在 `logs/worker.pid`
- `deploy/ensure-worker.sh`：幂等拉起；已挂在 `/etc/profile.d/etsy-analyzer-worker.sh` 与 `~/.bashrc`，box 重启后第一次开 shell 即自动启动
- 停止：`pkill -f run-worker.sh; pkill -f analyzer.main`；查看：`pgrep -af analyzer.main; tail -f logs/worker.log`

有 systemd 的主机请用 `deploy/etsy-analyzer-worker.service`（路径已指向本目录）。
