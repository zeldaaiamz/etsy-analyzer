-- 001_init.sql  Etsy → Amazon 选品分析 初始化
create extension if not exists pgcrypto;

create type job_status as enum ('queued','running','succeeded','failed','canceled');

create table public.batches (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  name text,
  created_at timestamptz not null default now()
);

create table public.jobs (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.batches(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  etsy_url text not null check (etsy_url ~* '^https?://([a-z]+\.)?etsy\.com/'),
  etsy_listing_id bigint,
  status job_status not null default 'queued',
  stage text,                       -- fetch_etsy / analyze / search_amazon / score
  attempts int not null default 0,
  max_attempts int not null default 3,
  error text,
  locked_by text,
  locked_at timestamptz,
  run_after timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  finished_at timestamptz
);
create index jobs_queue_idx on public.jobs (status, run_after, created_at) where status = 'queued';
create index jobs_batch_idx on public.jobs (batch_id);

create table public.etsy_products (
  job_id uuid primary key references public.jobs(id) on delete cascade,
  user_id uuid not null,
  listing_id bigint,
  title text, description text, price numeric, currency text,
  tags text[], materials text[], shop_name text, image_url text,
  num_favorers int, raw jsonb,
  fetched_at timestamptz not null default now()
);

create table public.analyses (
  job_id uuid primary key references public.jobs(id) on delete cascade,
  user_id uuid not null,
  product_summary text,          -- 产品是什么
  category text,
  target_audience text,          -- 目标人群
  keywords text[],               -- 候选亚马逊搜索词
  competition_level text check (competition_level in ('low','medium','high')),
  demand_level text check (demand_level in ('low','medium','high')),
  opportunity_score int check (opportunity_score between 0 and 100),
  verdict text,
  llm_model text, raw jsonb,
  created_at timestamptz not null default now()
);

create table public.amazon_matches (
  id bigserial primary key,
  job_id uuid not null references public.jobs(id) on delete cascade,
  user_id uuid not null,
  keyword text, asin text not null, title text, price numeric,
  rating numeric, review_count int, bsr int, bsr_category text,
  est_monthly_sales int,          -- 估算值（BSR 推算），非亚马逊官方数据
  similarity numeric, url text, image_url text, provider text, raw jsonb,
  created_at timestamptz not null default now()
);
create index amazon_matches_job_idx on public.amazon_matches(job_id);

-- updated_at
create or replace function public.touch_updated_at() returns trigger language plpgsql set search_path = public as $$
begin new.updated_at = now(); return new; end $$;
revoke all on function public.touch_updated_at() from public, anon, authenticated;
create trigger jobs_touch before update on public.jobs for each row execute function public.touch_updated_at();

-- ===== RLS：用户只能看/建自己的数据；结果表只能由 worker(service_role) 写 =====
alter table public.batches enable row level security;
alter table public.jobs enable row level security;
alter table public.etsy_products enable row level security;
alter table public.analyses enable row level security;
alter table public.amazon_matches enable row level security;

create policy batches_select on public.batches for select to authenticated using (user_id = (select auth.uid()));
create policy batches_insert on public.batches for insert to authenticated with check (user_id = (select auth.uid()));
create policy batches_delete on public.batches for delete to authenticated using (user_id = (select auth.uid()));

create policy jobs_select on public.jobs for select to authenticated using (user_id = (select auth.uid()));
create policy jobs_insert on public.jobs for insert to authenticated with check (
  user_id = (select auth.uid()) and status = 'queued' and attempts = 0
  and exists (select 1 from public.batches b where b.id = batch_id and b.user_id = (select auth.uid())));
-- 用户不能 update 任务（防止篡改状态）；取消请走 cancel_job()

create policy etsy_select on public.etsy_products for select to authenticated using (user_id = (select auth.uid()));
create policy analyses_select on public.analyses for select to authenticated using (user_id = (select auth.uid()));
create policy matches_select on public.amazon_matches for select to authenticated using (user_id = (select auth.uid()));

-- 用户取消自己排队中的任务
create or replace function public.cancel_job(p_job uuid) returns void
language sql security definer set search_path = public as $$
  update public.jobs set status='canceled', finished_at=now()
   where id = p_job and user_id = auth.uid() and status = 'queued';
$$;
revoke all on function public.cancel_job(uuid) from public, anon;
grant execute on function public.cancel_job(uuid) to authenticated;

-- ===== 队列：worker 原子领取下一个任务 =====
create or replace function public.claim_next_job(p_worker text, p_lock_timeout interval default interval '15 minutes')
returns setof public.jobs
language plpgsql security definer set search_path = public as $$
begin
  -- 回收超时的 running 任务（worker 崩溃）
  update public.jobs set status='queued', locked_by=null, locked_at=null
   where status='running' and locked_at < now() - p_lock_timeout;

  return query
  update public.jobs j set status='running', locked_by=p_worker, locked_at=now(),
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

-- ===== Realtime =====
alter publication supabase_realtime add table public.jobs, public.analyses, public.amazon_matches;
