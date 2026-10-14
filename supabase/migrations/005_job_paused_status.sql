-- 005：新增 paused 状态（单独迁移：新枚举值需先提交才能在函数/约束中使用）
alter type public.job_status add value if not exists 'paused';
