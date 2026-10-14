-- 010：按 EHunt 每日额度排程（追加式，旧前端/旧 worker 仍可工作）
-- 1) 单次提交 ≤500、排队+运行中 ≤1000，取消每日提交上限
-- 2) service_status 记录 EHunt 今日 used/remaining（前端额度条，authenticated 可读，已有策略）
-- 3) claim_next_job：熔断中仍领取 24h 缓存内的 listing（0 credit）；熔断到期自动恢复

create or replace function public.enforce_job_limits() returns trigger
language plpgsql security definer set search_path = public as $$
declare n_batch int; n_active int;
begin
  if auth.uid() is null then return new; end if;  -- service_role / 后台不受限
  select count(*) into n_batch from public.jobs where batch_id = new.batch_id;
  if n_batch >= 500 then raise exception '单次最多提交 500 条' using errcode = 'P0001'; end if;
  select count(*) into n_active from public.jobs where user_id = new.user_id and status in ('queued','running');
  if n_active >= 1000 then raise exception '排队中+运行中的任务已达上限 1000 条，请等待完成后再提交' using errcode = 'P0001'; end if;
  return new;
end $$;
revoke all on function public.enforce_job_limits() from public, anon, authenticated;

create index if not exists jobs_batch_idx on public.jobs (batch_id);
create index if not exists jobs_queue_idx on public.jobs (created_at) where status = 'queued';

alter table public.service_status add column if not exists ehunt_used_today int;
alter table public.service_status add column if not exists ehunt_daily_limit int not null default 200;
alter table public.service_status add column if not exists ehunt_quota_at timestamptz;

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

  -- 熔断判断每次领取都实时比较 now()：到期即自动恢复，无需人工清除
  select coalesce(bool_or(ehunt_paused_until > now()), false) into paused from public.service_status;

  return query
  update public.jobs j set status='running', locked_by=p_worker, locked_at=now(), started_at=now(), finished_at=null,
         attempts = j.attempts + 1, stage='fetch_etsy', error=null
   where j.id = (
     select q.id from public.jobs q
      where q.status='queued' and not q.cancel_requested
        and (q.run_after <= now() or (paused and coalesce(q.error, '') like '%额度%'))
        and (not paused or exists (   -- 熔断中：只放行 24h 缓存命中的 listing（不耗 EHunt 额度）
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
