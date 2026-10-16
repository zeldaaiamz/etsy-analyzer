-- 027：预筛选「一键刷新」——把所有仍有剩余次数的取数失败项重新排队。规则与 026 screen_refetch 完全相同：
--   不重置 attempts；仅 status='failed'、prescreen、screen_state 非 'kept'、未取消、attempts < max_attempts；可选按批次。
-- 纯追加：新函数，不改动任何现有对象。
create or replace function public.screen_refetch_all(p_batch uuid default null)
returns int language sql security definer set search_path = public as $$
  with u as (update public.jobs set status = 'queued', error = null, stage = null, finished_at = null,
                    locked_by = null, locked_at = null, queued_at = now(), run_after = now()
              where user_id = auth.uid() and prescreen and status = 'failed' and screen_hidden_at is null
                and screen_state is distinct from 'kept' and not cancel_requested and attempts < max_attempts
                and (p_batch is null or batch_id = p_batch)
              returning 1)
  select count(*)::int from u;
$$;
revoke all on function public.screen_refetch_all(uuid) from public, anon;
grant execute on function public.screen_refetch_all(uuid) to authenticated;
