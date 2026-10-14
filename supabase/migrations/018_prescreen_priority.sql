-- 018：claim_next_job_v2 优先领取预筛选取数段任务（只调 EHunt，几秒完成），再领全流程分析任务。
-- 纯增量：仅替换函数体中的排序，签名/权限不变。
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
      order by (case when q.prescreen and q.screen_state is distinct from 'kept' then 0 else 1 end), q.created_at
      for update skip locked
      limit 1)
  returning j.*;
end $$;
create index if not exists jobs_queued_prio_idx on public.jobs ((case when prescreen and screen_state is distinct from 'kept' then 0 else 1 end), created_at) where status = 'queued';
