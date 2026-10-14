-- 007：QA 加固（全部为追加式改动，旧版前端/旧 worker 仍可工作）
-- 1) 取消运行中任务  2) 卡死回收尊重 attempts  3) 提交上限  4) 供应商缓存  5) 心跳/熔断/用量  6) 任务更新只走 RPC  7) 允许 etsy.me

-- ---------- jobs 新列 ----------
alter table public.jobs add column if not exists cancel_requested boolean not null default false;
create index if not exists jobs_user_listing_idx on public.jobs (user_id, etsy_listing_id);
create index if not exists jobs_user_created_idx on public.jobs (user_id, created_at desc);

-- etsy.me 短链 / m.etsy.com
alter table public.jobs drop constraint if exists jobs_etsy_url_check;
alter table public.jobs add constraint jobs_etsy_url_check
  check (etsy_url ~* '^https?://(([a-z]+\.)?etsy\.com|etsy\.me)/');

-- ---------- 提交上限（DB 强制）----------
create or replace function public.enforce_job_limits() returns trigger
language plpgsql security definer set search_path = public as $$
declare n_batch int; n_active int; n_day int;
begin
  if auth.uid() is null then return new; end if;  -- service_role / 后台不受限
  select count(*) into n_batch from public.jobs where batch_id = new.batch_id;
  if n_batch >= 50 then raise exception '单次最多提交 50 条' using errcode = 'P0001'; end if;
  select count(*) into n_active from public.jobs where user_id = new.user_id and status in ('queued','running');
  if n_active >= 100 then raise exception '排队中+运行中的任务已达上限 100 条，请等待完成后再提交' using errcode = 'P0001'; end if;
  select count(*) into n_day from public.jobs where user_id = new.user_id and created_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc';
  if n_day >= 150 then raise exception '今日（UTC）提交已达上限 150 条，请明天再试' using errcode = 'P0001'; end if;
  return new;
end $$;
revoke all on function public.enforce_job_limits() from public, anon, authenticated;
drop trigger if exists jobs_limits on public.jobs;
create trigger jobs_limits before insert on public.jobs for each row execute function public.enforce_job_limits();

-- ---------- 服务状态（熔断）/ worker 心跳 / 每日用量 / 缓存 ----------
create table if not exists public.service_status (
  id int primary key default 1 check (id = 1),
  ehunt_paused_until timestamptz,
  ehunt_remaining int,
  reason text,
  updated_at timestamptz not null default now());
insert into public.service_status (id) values (1) on conflict do nothing;

create table if not exists public.worker_heartbeat (
  worker_id text primary key,
  last_seen timestamptz not null default now(),
  current_job uuid,
  info jsonb);

create table if not exists public.provider_usage_daily (
  day date not null default (now() at time zone 'utc')::date,
  provider text not null,
  calls int not null default 0,
  units int not null default 0,
  cache_hits int not null default 0,
  primary key (day, provider));

create table if not exists public.provider_cache (
  key text primary key,
  provider text not null,
  payload jsonb not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null);
create index if not exists provider_cache_exp_idx on public.provider_cache (expires_at);

alter table public.service_status enable row level security;
alter table public.worker_heartbeat enable row level security;
alter table public.provider_usage_daily enable row level security;
alter table public.provider_cache enable row level security;   -- 无策略 = 仅 service_role
create policy service_status_read on public.service_status for select to authenticated using (true);
create policy worker_heartbeat_read on public.worker_heartbeat for select to authenticated using (true);
create policy usage_read on public.provider_usage_daily for select to authenticated using (true);
revoke insert, update, delete on public.service_status, public.worker_heartbeat, public.provider_usage_daily, public.provider_cache from anon, authenticated;
revoke select on public.provider_cache from anon, authenticated;

create or replace function public.bump_usage(p_provider text, p_calls int, p_units int, p_hits int default 0) returns void
language sql security definer set search_path = public as $$
  insert into public.provider_usage_daily (day, provider, calls, units, cache_hits)
  values ((now() at time zone 'utc')::date, p_provider, p_calls, p_units, p_hits)
  on conflict (day, provider) do update set calls = provider_usage_daily.calls + excluded.calls,
    units = provider_usage_daily.units + excluded.units, cache_hits = provider_usage_daily.cache_hits + excluded.cache_hits;
$$;
revoke all on function public.bump_usage(text, int, int, int) from public, anon, authenticated;
grant execute on function public.bump_usage(text, int, int, int) to service_role;

-- ---------- 领取：卡死回收尊重 attempts；熔断期间不领取；跳过已请求取消 ----------
create or replace function public.claim_next_job(p_worker text, p_lock_timeout interval default interval '15 minutes')
returns setof public.jobs
language plpgsql security definer set search_path = public as $$
begin
  -- 心跳（locked_at，每个阶段刷新）超时：达到最大次数或已请求取消 → 终止；否则重新排队
  update public.jobs set status = case when cancel_requested then 'canceled'::job_status else 'failed'::job_status end,
         error = case when cancel_requested then error else 'worker 超时（多次处理未完成，已停止重试）' end,
         finished_at = now(), locked_by = null
   where status = 'running' and locked_at < now() - p_lock_timeout and (attempts >= max_attempts or cancel_requested);
  update public.jobs set status = 'queued', locked_by = null, locked_at = null, queued_at = now(),
         error = 'worker 超时，已重新排队'
   where status = 'running' and locked_at < now() - p_lock_timeout;

  if exists (select 1 from public.service_status where ehunt_paused_until > now()) then
    return;  -- EHunt 熔断中：任务保持排队
  end if;

  return query
  update public.jobs j set status='running', locked_by=p_worker, locked_at=now(), started_at=now(), finished_at=null,
         attempts = j.attempts + 1, stage='fetch_etsy', error=null
   where j.id = (
     select id from public.jobs
      where status='queued' and run_after <= now() and not cancel_requested
      order by created_at
      for update skip locked
      limit 1)
  returning j.*;
end $$;
revoke all on function public.claim_next_job(text, interval) from public, anon, authenticated;
grant execute on function public.claim_next_job(text, interval) to service_role;

-- ---------- 用户 RPC ----------
-- 取消：排队/暂停 → 立即取消；运行中 → 设置 cancel_requested，worker 在阶段之间检查
create or replace function public.request_cancel(p_job uuid) returns text
language plpgsql security definer set search_path = public as $$
declare st job_status;
begin
  select status into st from public.jobs where id = p_job and user_id = (select auth.uid()) for update;
  if st in ('queued','paused') then
    update public.jobs set status='canceled', finished_at=now() where id = p_job; return 'canceled';
  elsif st = 'running' then
    update public.jobs set cancel_requested = true where id = p_job; return 'requested';
  end if;
  return 'noop';
end $$;
create or replace function public.cancel_job(p_job uuid) returns void
language sql security definer set search_path = public as $$
  update public.jobs set status='canceled', finished_at=now()
   where id = p_job and user_id = (select auth.uid()) and status in ('queued','paused');
$$;
create or replace function public.resume_job(p_job uuid) returns boolean
language sql security definer set search_path = public as $$
  with u as (update public.jobs set status='queued', queued_at=now(), run_after=now(), cancel_requested=false
              where id=p_job and user_id=(select auth.uid()) and status='paused' returning 1)
  select exists(select 1 from u);
$$;
-- 重试/重新分析：失败、取消、已完成均可
create or replace function public.retry_job(p_job uuid) returns boolean
language sql security definer set search_path = public as $$
  with u as (update public.jobs set status='queued', attempts=0, error=null, stage=null, queued_at=now(), run_after=now(),
                    started_at=null, finished_at=null, reviewed_at=null, locked_by=null, locked_at=null, cancel_requested=false
              where id=p_job and user_id=(select auth.uid()) and status in ('failed','canceled','succeeded') returning 1)
  select exists(select 1 from u);
$$;
do $$ declare f text; begin
  foreach f in array array['request_cancel','cancel_job','resume_job','retry_job'] loop
    execute format('revoke all on function public.%I(uuid) from public, anon', f);
    execute format('grant execute on function public.%I(uuid) to authenticated', f);
  end loop;
end $$;

-- ---------- 任务更新只走 RPC ----------
drop policy if exists jobs_cancel on public.jobs;
revoke update on public.jobs from anon, authenticated;

-- realtime：服务状态/心跳变化推送到前端
alter publication supabase_realtime add table public.service_status;
