-- 017：预筛选流程（纯追加 / 向后兼容）
-- jobs.prescreen=true 的任务分两段：
--   取数段：worker 只调 EHunt（缓存优先）→ status='screening', screen_state='awaiting'
--   用户在筛选页确认 screen_confirm() → 保留的 screen_state='kept' 重新排队；排除的 status='canceled', screen_state='rejected'
--   分析段：worker 读已存的 etsy_products（不再调 EHunt）→ DeepSeek / Keepamore / 截图
-- 旧 worker 的 claim_next_job 跳过 prescreen 任务（现存任务全部 prescreen=false，行为不变）。
alter table public.jobs add column if not exists prescreen boolean not null default false;
alter table public.jobs add column if not exists screen_state text
  check (screen_state is null or screen_state in ('awaiting', 'kept', 'rejected'));
alter table public.jobs add column if not exists screened_at timestamptz;
create index if not exists jobs_screen_idx on public.jobs (user_id, batch_id) where prescreen;

-- 任务列表：隐藏尚未确认 / 已排除的预筛选任务（它们只出现在筛选页；已排除可在筛选页“已排除”里查看与恢复）
create or replace view public.task_jobs with (security_invoker = true) as
select j.* from public.jobs j
 where (not j.prescreen or j.screen_state = 'kept')
   and (j.etsy_listing_id is null
    or not exists (select 1 from public.product_library l where l.user_id = j.user_id and l.etsy_listing_id = j.etsy_listing_id));
revoke all on public.task_jobs from anon;
grant select on public.task_jobs to authenticated;

-- 旧版领取：与 010 相同，仅多 `not q.prescreen`（旧 worker 不会把预筛选任务跑成全流程）
create or replace function public.claim_next_job(p_worker text, p_lock_timeout interval default interval '15 minutes')
returns setof public.jobs
language plpgsql security definer set search_path = public as $$
declare paused boolean;
begin
  update public.jobs set status = case when cancel_requested then 'canceled'::job_status else 'failed'::job_status end,
         error = case when cancel_requested then error else 'worker 超时（多次处理未完成，已停止重试）' end,
         finished_at = now(), locked_by = null
   where status = 'running' and locked_at < now() - p_lock_timeout and (attempts >= max_attempts or cancel_requested);
  update public.jobs set status = 'queued', locked_by = null, locked_at = null, queued_at = now(),
         error = 'worker 超时，已重新排队'
   where status = 'running' and locked_at < now() - p_lock_timeout;
  select coalesce(bool_or(ehunt_paused_until > now()), false) into paused from public.service_status;
  return query
  update public.jobs j set status='running', locked_by=p_worker, locked_at=now(), started_at=now(), finished_at=null,
         attempts = j.attempts + 1, stage='fetch_etsy', error=null
   where j.id = (
     select q.id from public.jobs q
      where q.status='queued' and not q.cancel_requested and not q.prescreen
        and (q.run_after <= now() or (paused and coalesce(q.error, '') like '%额度%'))
        and (not paused or exists (
              select 1 from public.provider_cache c
               where c.key = 'ehunt:listing:' || coalesce(q.etsy_listing_id::text, substring(q.etsy_url from '/listing/(\d+)'))
                 and c.expires_at > now()))
      order by q.created_at
      for update skip locked
      limit 1)
  returning j.*;
end $$;
revoke all on function public.claim_next_job(text, interval) from public, anon, authenticated;
grant execute on function public.claim_next_job(text, interval) to service_role;

-- 新版领取（新 worker 使用）：包含预筛选任务。
-- 已确认保留（kept）的任务 EHunt 数据已入库 → 熔断中也放行（0 credit），阶段直接从 analyze 开始。
create or replace function public.claim_next_job_v2(p_worker text, p_lock_timeout interval default interval '15 minutes')
returns setof public.jobs
language plpgsql security definer set search_path = public as $$
declare paused boolean;
begin
  update public.jobs set status = case when cancel_requested then 'canceled'::job_status else 'failed'::job_status end,
         error = case when cancel_requested then error else 'worker 超时（多次处理未完成，已停止重试）' end,
         finished_at = now(), locked_by = null
   where status = 'running' and locked_at < now() - p_lock_timeout and (attempts >= max_attempts or cancel_requested);
  update public.jobs set status = 'queued', locked_by = null, locked_at = null, queued_at = now(),
         error = 'worker 超时，已重新排队'
   where status = 'running' and locked_at < now() - p_lock_timeout;
  select coalesce(bool_or(ehunt_paused_until > now()), false) into paused from public.service_status;
  return query
  update public.jobs j set status='running', locked_by=p_worker, locked_at=now(), started_at=now(), finished_at=null,
         attempts = j.attempts + 1, stage = case when j.screen_state = 'kept' then 'analyze' else 'fetch_etsy' end, error=null
   where j.id = (
     select q.id from public.jobs q
      where q.status='queued' and not q.cancel_requested
        and (q.run_after <= now() or (paused and coalesce(q.error, '') like '%额度%'))
        and (not paused or q.screen_state = 'kept' or exists (
              select 1 from public.provider_cache c
               where c.key = 'ehunt:listing:' || coalesce(q.etsy_listing_id::text, substring(q.etsy_url from '/listing/(\d+)'))
                 and c.expires_at > now()))
      order by q.created_at
      for update skip locked
      limit 1)
  returning j.*;
end $$;
revoke all on function public.claim_next_job_v2(text, interval) from public, anon, authenticated;
grant execute on function public.claim_next_job_v2(text, interval) to service_role;

-- 用户确认一批：p_keep 进入分析段，p_reject 停止（不再产生任何调用）。只作用于本人、status='screening' 的任务。
create or replace function public.screen_confirm(p_keep uuid[], p_reject uuid[] default '{}')
returns jsonb language plpgsql security definer set search_path = public as $$
declare nk int; nr int;
begin
  if auth.uid() is null then raise exception 'not authenticated' using errcode = '42501'; end if;
  with u as (update public.jobs set status = 'queued', screen_state = 'kept', screened_at = now(), attempts = 0,
                    error = null, stage = null, run_after = now(), queued_at = now(), finished_at = null
              where id = any(coalesce(p_keep, '{}')) and user_id = auth.uid() and prescreen and status = 'screening' returning 1)
  select count(*) into nk from u;
  with u as (update public.jobs set status = 'canceled', screen_state = 'rejected', screened_at = now(),
                    finished_at = now(), error = '筛选时已排除'
              where id = any(coalesce(p_reject, '{}')) and user_id = auth.uid() and prescreen and status = 'screening' returning 1)
  select count(*) into nr from u;
  return jsonb_build_object('kept', nk, 'rejected', nr);
end $$;
revoke all on function public.screen_confirm(uuid[], uuid[]) from public, anon;
grant execute on function public.screen_confirm(uuid[], uuid[]) to authenticated;

-- 恢复已排除：回到待确认（EHunt 数据仍在，不重新取数）
create or replace function public.screen_restore(p_ids uuid[])
returns int language sql security definer set search_path = public as $$
  with u as (update public.jobs set status = 'screening', screen_state = 'awaiting', screened_at = null, finished_at = null, error = null
              where id = any(p_ids) and user_id = auth.uid() and prescreen and screen_state = 'rejected' returning 1)
  select count(*)::int from u;
$$;
revoke all on function public.screen_restore(uuid[]) from public, anon;
grant execute on function public.screen_restore(uuid[]) to authenticated;

-- 筛选页数据：预筛选中（取数中 / 待确认 / 已排除）的任务 + 卡片所需的 Etsy 字段
create or replace function public.screen_list(p_batch uuid default null)
returns jsonb language sql stable security invoker set search_path = public as $$
  select coalesce(jsonb_agg(jsonb_build_object('id', j.id, 'batch_id', j.batch_id, 'etsy_url', j.etsy_url, 'status', j.status,
           'screen_state', j.screen_state, 'error', j.error, 'run_after', j.run_after, 'created_at', j.created_at,
           'title', e.title, 'image_url', e.image_url, 'price', e.price, 'currency', e.currency, 'listing_id', coalesce(e.listing_id, j.etsy_listing_id))
           order by j.created_at, j.id), '[]'::jsonb)
    from public.jobs j left join public.etsy_products e on e.job_id = j.id
   where j.user_id = auth.uid() and j.prescreen and coalesce(j.screen_state, '') <> 'kept'
     and (p_batch is null or j.batch_id = p_batch);
$$;
revoke all on function public.screen_list(uuid) from public, anon;
grant execute on function public.screen_list(uuid) to authenticated;
