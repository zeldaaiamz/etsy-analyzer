-- 040：预筛选「删除所选」（勾选多条，可跨批次，一次调用）。未在生产执行。
-- 走 023 的可恢复路径：只设 jobs.screen_hidden_at = now()（screen_list 已过滤隐藏行），不删任务 / EHunt 数据，不消耗额度；
-- 撤销用 023 已有的 screen_unhide(p_ids)（025 之后仍保留在库中）。
-- 只隐藏本人、prescreen、未进入分析（screen_state <> 'kept'）、且不在取数中的条目：
--   screening（待确认 / 未确认的 ✕）、failed（取数失败）、canceled（已确认排除）。queued / running 不隐藏（worker 还会继续取数并消耗额度，应先等结果）。
-- 幂等：已隐藏的不重复计数，连点 / 两个标签页重复提交只生效一次。
create or replace function public.screen_hide_ids(p_ids uuid[])
returns jsonb language sql security definer set search_path = public as $$
  with u as (update public.jobs set screen_hidden_at = now()
              where id = any(coalesce(p_ids, '{}')) and user_id = auth.uid() and prescreen and screen_hidden_at is null
                and coalesce(screen_state, '') <> 'kept' and status in ('screening', 'failed', 'canceled')
              returning id, batch_id)
  select jsonb_build_object('hidden', count(*), 'ids', coalesce(jsonb_agg(id), '[]'::jsonb)) from u;
$$;
revoke all on function public.screen_hide_ids(uuid[]) from public, anon;
grant execute on function public.screen_hide_ids(uuid[]) to authenticated;
