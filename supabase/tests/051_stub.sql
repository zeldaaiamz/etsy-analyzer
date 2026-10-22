-- 051 本地桩：与生产同名同列（只建 051 用到的部分）；task_jobs / screen_confirm / screen_unhide 为生产当前定义
create role anon; create role authenticated; create role service_role;
create schema auth; create table auth.users (id uuid primary key);
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
create type public.job_status as enum ('queued','running','succeeded','failed','canceled','screening');
create table public.batches (id uuid primary key default gen_random_uuid(), user_id uuid not null default auth.uid(), name text, created_at timestamptz not null default now());
create table public.jobs (id uuid primary key default gen_random_uuid(), batch_id uuid references public.batches(id), user_id uuid not null default auth.uid(),
  etsy_url text not null default 'https://www.etsy.com/listing/1', etsy_listing_id bigint, status public.job_status not null default 'queued', stage text, attempts int not null default 0,
  max_attempts int not null default 3, error text, locked_by text, locked_at timestamptz, run_after timestamptz default now(), created_at timestamptz not null default now(),
  updated_at timestamptz default now(), finished_at timestamptz, queued_at timestamptz, started_at timestamptz, reviewed_at timestamptz, cancel_requested boolean not null default false,
  prescreen boolean not null default false, screen_state text, screened_at timestamptz, screen_hidden_at timestamptz);
create table public.etsy_products (job_id uuid primary key, title text, image_url text, price numeric, shop_name text, listing_id bigint, raw jsonb);
create table public.analyses (job_id uuid, product_summary text, target_audience text, keywords text[], decision_trace jsonb, audience_evidence jsonb);
create table public.evidence_snapshots (job_id uuid, kind text, asin text);
create table public.amazon_matches (job_id uuid, asin text);
create table public.product_library (id uuid primary key default gen_random_uuid(), user_id uuid not null, job_id uuid, etsy_listing_id bigint, unique (user_id, etsy_listing_id));
create view public.task_jobs as select id, batch_id, user_id, etsy_url, etsy_listing_id, status, stage, attempts, max_attempts, error, locked_by, locked_at, run_after, created_at, updated_at,
  finished_at, queued_at, started_at, reviewed_at, cancel_requested, prescreen, screen_state, screened_at from jobs j
  where ((not prescreen) or (screen_state = 'kept')) and ((etsy_listing_id is null) or (not exists (select 1 from product_library l where l.user_id = j.user_id and l.etsy_listing_id = j.etsy_listing_id)));
create function public.screen_confirm(p_keep uuid[], p_reject uuid[] default '{}') returns jsonb language plpgsql security definer set search_path to 'public' as $$
declare nk int; nr int; begin
  with u as (update public.jobs set status = 'queued', screen_state = 'kept', screened_at = now(), attempts = 0, error = null, stage = null, run_after = now(), queued_at = now(), finished_at = null
              where id = any(coalesce(p_keep, '{}')) and user_id = auth.uid() and prescreen and status = 'screening' returning 1) select count(*) into nk from u;
  with u as (update public.jobs set status = 'canceled', screen_state = 'rejected', screened_at = now(), finished_at = now(), error = '筛选时已排除'
              where id = any(coalesce(p_reject, '{}')) and user_id = auth.uid() and prescreen and status = 'screening' returning 1) select count(*) into nr from u;
  return jsonb_build_object('kept', nk, 'rejected', nr); end $$;
create function public.screen_unhide(p_ids uuid[] default null) returns integer language sql security definer set search_path to 'public' as $$
  with u as (update public.jobs set screen_hidden_at = null where user_id = auth.uid() and prescreen and screen_hidden_at is not null and (p_ids is null or id = any(p_ids)) returning 1)
  select count(*)::int from u; $$;
create function public.task_list(p_limit integer default 30, p_ids uuid[] default null) returns jsonb language sql as $$ select '{}'::jsonb $$;
