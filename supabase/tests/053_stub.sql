-- 053 本地桩：jobs / etsy_products / ehunt_lookup_requests / provider_cache（够跑 screen_refetch_got）
create role anon; create role authenticated; create role service_role;
create schema auth; create table auth.users (id uuid primary key);
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
create type public.job_status as enum ('queued','running','succeeded','failed','canceled','screening');
create table public.batches (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid(),
  name text,
  created_at timestamptz not null default now()
);
create table public.jobs (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid references public.batches(id),
  user_id uuid not null default auth.uid(),
  etsy_url text not null default 'https://www.etsy.com/listing/1',
  etsy_listing_id bigint,
  status public.job_status not null default 'queued',
  stage text,
  attempts int not null default 0,
  max_attempts int not null default 3,
  error text,
  locked_by text,
  locked_at timestamptz,
  run_after timestamptz default now(),
  created_at timestamptz not null default now(),
  finished_at timestamptz,
  queued_at timestamptz,
  cancel_requested boolean not null default false,
  prescreen boolean not null default false,
  screen_state text,
  screen_hidden_at timestamptz
);
create table public.etsy_products (
  job_id uuid primary key references public.jobs(id) on delete cascade,
  user_id uuid,
  listing_id bigint,
  title text,
  image_url text,
  price numeric,
  raw jsonb
);
create table public.ehunt_lookup_requests (
  listing_id bigint primary key,
  status text not null default 'queued'
    check (status in ('queued','running','done','not_found','login_expired','error')),
  image_url text,
  error text,
  attempts int not null default 0,
  requested_at timestamptz not null default now(),
  claimed_by text,
  claimed_at timestamptz,
  finished_at timestamptz
);
create table public.provider_cache (
  key text primary key,
  provider text,
  payload jsonb,
  expires_at timestamptz
);
-- 产品库：053 不得触碰；建表仅用于断言
create table public.product_library (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  job_id uuid,
  etsy_listing_id bigint,
  unique (user_id, etsy_listing_id)
);
-- 关键词永久日志：053 不得触碰
create table public.sel_keyword_log (
  id bigserial primary key,
  keyword text not null,
  created_at timestamptz not null default now()
);
