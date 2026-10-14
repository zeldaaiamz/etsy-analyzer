-- 006：任务面板（暂停/继续/重试/已查看）+ 耗时统计
alter table public.jobs
  add column if not exists queued_at   timestamptz not null default now(), -- 最近一次进入队列（创建/重试/继续）
  add column if not exists started_at  timestamptz,                        -- 最近一次被 worker 领取
  add column if not exists reviewed_at timestamptz;                        -- 用户查看过结果（null = 待我处理）
update public.jobs set queued_at = created_at where queued_at > created_at;
update public.jobs set reviewed_at = coalesce(finished_at, now()) where status = 'succeeded' and reviewed_at is null; -- 历史结果视为已查看

-- 领取：只领 queued（paused 不会被领），领取即置 running 并记录 started_at
create or replace function public.claim_next_job(p_worker text, p_lock_timeout interval default interval '15 minutes')
returns setof public.jobs
language plpgsql security definer set search_path = public as $$
begin
  update public.jobs set status='queued', locked_by=null, locked_at=null, queued_at=now()
   where status='running' and locked_at < now() - p_lock_timeout;

  return query
  update public.jobs j set status='running', locked_by=p_worker, locked_at=now(), started_at=now(), finished_at=null,
         attempts = j.attempts + 1, stage='fetch_etsy', error=null
   where j.id = (
     select id from public.jobs
      where status='queued' and run_after <= now()
      order by created_at
      for update skip locked
      limit 1)
  returning j.*;
end $$;
revoke all on function public.claim_next_job(text, interval) from public, anon, authenticated;
grant execute on function public.claim_next_job(text, interval) to service_role;

-- 用户操作：SECURITY DEFINER + 严格 where（仅本人、仅允许的状态迁移、仅这些列）
create or replace function public.pause_job(p_job uuid) returns boolean
language sql security definer set search_path = public as $$
  with u as (update public.jobs set status='paused'
              where id=p_job and user_id=(select auth.uid()) and status='queued' returning 1)
  select exists(select 1 from u);
$$;
create or replace function public.resume_job(p_job uuid) returns boolean
language sql security definer set search_path = public as $$
  with u as (update public.jobs set status='queued', queued_at=now(), run_after=now()
              where id=p_job and user_id=(select auth.uid()) and status='paused' returning 1)
  select exists(select 1 from u);
$$;
create or replace function public.retry_job(p_job uuid) returns boolean
language sql security definer set search_path = public as $$
  with u as (update public.jobs set status='queued', attempts=0, error=null, stage=null, queued_at=now(), run_after=now(),
                    started_at=null, finished_at=null, reviewed_at=null, locked_by=null, locked_at=null
              where id=p_job and user_id=(select auth.uid()) and status in ('failed','canceled') returning 1)
  select exists(select 1 from u);
$$;
create or replace function public.mark_job_reviewed(p_job uuid) returns boolean
language sql security definer set search_path = public as $$
  with u as (update public.jobs set reviewed_at=now()
              where id=p_job and user_id=(select auth.uid()) and status='succeeded' and reviewed_at is null returning 1)
  select exists(select 1 from u);
$$;
do $$ declare f text; begin
  foreach f in array array['pause_job','resume_job','retry_job','mark_job_reviewed'] loop
    execute format('revoke all on function public.%I(uuid) from public, anon', f);
    execute format('grant execute on function public.%I(uuid) to authenticated', f);
  end loop;
end $$;
