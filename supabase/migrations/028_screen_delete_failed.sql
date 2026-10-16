-- 028：预筛选「一键删除失败」——硬删除已用完重试次数的取数失败项（「取数失败（已重试 3 次，已停止）」）。不可恢复。
-- 语义同 025 screen_hide_rejected：删除 public.jobs 行，子表 on delete cascade，product_library.job_id set null。
-- 只删本人、prescreen、status='failed'、attempts >= max_attempts、screen_state 非 'kept' 的任务（从未进入分析，不涉及 storage）。
-- 纯追加：新函数，不改动现有对象。
create or replace function public.screen_delete_failed(p_batch uuid default null)
returns jsonb language sql security definer set search_path = public as $$
  with d as (delete from public.jobs
              where user_id = auth.uid() and prescreen and status = 'failed' and attempts >= max_attempts
                and screen_state is distinct from 'kept' and screen_hidden_at is null
                and (p_batch is null or batch_id = p_batch)
              returning id)
  select jsonb_build_object('deleted', count(*), 'ids', coalesce(jsonb_agg(id), '[]'::jsonb)) from d;
$$;
revoke all on function public.screen_delete_failed(uuid) from public, anon;
grant execute on function public.screen_delete_failed(uuid) to authenticated;
