-- 023：预筛选页「一键删除所有已排除」= 软删除（隐藏），不删任何任务 / EHunt 数据，可恢复，不消耗 EHunt 额度。
-- 新列 jobs.screen_hidden_at：非空 = 已从筛选页移除。screen_list 不再返回这些；screen_restore 恢复时一并清除。
alter table public.jobs add column if not exists screen_hidden_at timestamptz;

-- 隐藏本人全部（或某一批）已排除的预筛选任务；返回 {hidden, ids}，ids 用于“撤销”
create or replace function public.screen_hide_rejected(p_batch uuid default null)
returns jsonb language sql security definer set search_path = public as $$
  with u as (update public.jobs set screen_hidden_at = now()
              where user_id = auth.uid() and prescreen and screen_state = 'rejected' and screen_hidden_at is null
                and (p_batch is null or batch_id = p_batch)
              returning id)
  select jsonb_build_object('hidden', count(*), 'ids', coalesce(jsonb_agg(id), '[]'::jsonb)) from u;
$$;

-- 恢复：把隐藏的已排除任务放回「已排除」标签（p_ids 为空 = 恢复本人全部隐藏的）
create or replace function public.screen_unhide(p_ids uuid[] default null)
returns int language sql security definer set search_path = public as $$
  with u as (update public.jobs set screen_hidden_at = null
              where user_id = auth.uid() and prescreen and screen_hidden_at is not null
                and (p_ids is null or id = any(p_ids)) returning 1)
  select count(*)::int from u;
$$;

-- screen_restore：同 017，另外清除隐藏标记
create or replace function public.screen_restore(p_ids uuid[])
returns int language sql security definer set search_path = public as $$
  with u as (update public.jobs set status = 'screening', screen_state = 'awaiting', screened_at = null, finished_at = null, error = null,
                    screen_hidden_at = null
              where id = any(p_ids) and user_id = auth.uid() and prescreen and screen_state = 'rejected' returning 1)
  select count(*)::int from u;
$$;

-- screen_list：同 017，排除已隐藏的；额外返回 hidden_count 不改变返回类型（仍是数组），隐藏数量用 screen_hidden_count()
create or replace function public.screen_list(p_batch uuid default null)
returns jsonb language sql stable security invoker set search_path = public as $$
  select coalesce(jsonb_agg(jsonb_build_object('id', j.id, 'batch_id', j.batch_id, 'etsy_url', j.etsy_url, 'status', j.status,
           'screen_state', j.screen_state, 'error', j.error, 'run_after', j.run_after, 'created_at', j.created_at,
           'title', e.title, 'image_url', e.image_url, 'price', e.price, 'currency', e.currency, 'listing_id', coalesce(e.listing_id, j.etsy_listing_id))
           order by j.created_at, j.id), '[]'::jsonb)
    from public.jobs j left join public.etsy_products e on e.job_id = j.id
   where j.user_id = auth.uid() and j.prescreen and coalesce(j.screen_state, '') <> 'kept' and j.screen_hidden_at is null
     and (p_batch is null or j.batch_id = p_batch);
$$;

create or replace function public.screen_hidden_count(p_batch uuid default null)
returns int language sql stable security invoker set search_path = public as $$
  select count(*)::int from public.jobs where user_id = auth.uid() and prescreen and screen_hidden_at is not null
     and (p_batch is null or batch_id = p_batch);
$$;

revoke all on function public.screen_hide_rejected(uuid) from public, anon;
revoke all on function public.screen_unhide(uuid[]) from public, anon;
revoke all on function public.screen_restore(uuid[]) from public, anon;
revoke all on function public.screen_list(uuid) from public, anon;
revoke all on function public.screen_hidden_count(uuid) from public, anon;
grant execute on function public.screen_hide_rejected(uuid) to authenticated;
grant execute on function public.screen_unhide(uuid[]) to authenticated;
grant execute on function public.screen_restore(uuid[]) to authenticated;
grant execute on function public.screen_list(uuid) to authenticated;
grant execute on function public.screen_hidden_count(uuid) to authenticated;
