-- 034 获取链接实时进度：box selection worker 节流写入（同阶段最多每 2 秒一次，阶段切换立即写），网页 3 秒轮询读取。
-- 结构见 worker/selection/ehunt_worker.py Progress：
--   {"stage": "count_open|count_filter|count_read|count_done|read_setup|reading|vision|done|canceled",
--    "batch_no", "target", "read", "judged", "vision_pending", "passed", "excluded", "dup", "page", "total", "at"}
-- 只加一列，旧 worker / 旧网页不受影响（未执行本迁移时新 worker 自动停写，网页回退到 sel_batches 的计数）。
alter table public.sel_runs add column if not exists progress jsonb;
comment on column public.sel_runs.progress is '获取链接实时进度（box worker 节流写入）；网页进度条读取';
