-- 025：预筛选「删除所有已排除」改为硬删除（替换 023 的软删除 screen_hidden_at）。不可恢复。
-- 删除 public.jobs 行；子表 etsy_products / analyses / matches / evidence 等 on delete cascade，product_library.job_id on delete set null。
-- 只删本人、prescreen、screen_state='rejected' 的任务（已排除的预筛任务从未进入 DeepSeek/截图，不涉及 storage 对象）。
-- 不清理历史软删除行（screen_hidden_at 非空）：screen_list 仍按 023 过滤它们；screen_unhide / screen_hidden_count 保留在库中但前端不再调用。
-- 返回值保留 'hidden' 键以兼容旧前端，新增 'deleted'。
create or replace function public.screen_hide_rejected(p_batch uuid default null)
returns jsonb language sql security definer set search_path = public as $$
  with d as (delete from public.jobs
              where user_id = auth.uid() and prescreen and screen_state = 'rejected' and screen_hidden_at is null
                and (p_batch is null or batch_id = p_batch)
              returning id)
  select jsonb_build_object('deleted', count(*), 'hidden', count(*), 'ids', coalesce(jsonb_agg(id), '[]'::jsonb)) from d;
$$;
revoke all on function public.screen_hide_rejected(uuid) from public, anon;
grant execute on function public.screen_hide_rejected(uuid) to authenticated;
