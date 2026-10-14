-- 016：预筛选状态（单独迁移：新枚举值需先提交才能在 017 的函数中使用）。纯追加，旧前端/旧 worker 不受影响。
alter type public.job_status add value if not exists 'screening';
