-- 055 本地桩：jobs / product_library / evidence_snapshots / listing_scrape_status（评论状态保留验证）
create role anon; create role authenticated; create role service_role;
create schema auth; create table auth.users (id uuid primary key);
insert into auth.users(id) values
  ('11111111-1111-1111-1111-111111111111'),
  ('22222222-2222-2222-2222-222222222222');
create function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
create type public.job_status as enum ('queued','running','succeeded','failed','canceled','screening');
create table public.batches (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid(),
  name text,
  created_at timestamptz not null default now()
);
create table public.jobs (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid references public.batches(id) on delete cascade,
  user_id uuid not null default auth.uid(),
  etsy_url text not null default 'https://www.etsy.com/listing/1',
  etsy_listing_id bigint,
  status public.job_status not null default 'queued',
  created_at timestamptz not null default now()
);
create table public.etsy_products (
  job_id uuid primary key references public.jobs(id) on delete cascade,
  title text, listing_id bigint
);
create table public.analyses (job_id uuid primary key references public.jobs(id) on delete cascade, product_summary text);
create table public.evidence_snapshots (
  id bigserial primary key,
  job_id uuid not null references public.jobs(id) on delete cascade,
  image_path text
);
create table public.product_library (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  job_id uuid references public.jobs(id) on delete set null,
  etsy_listing_id bigint,
  stage text not null default 'candidate',
  unique (user_id, etsy_listing_id)
);
create table public.listing_scrape_status (
  listing_id bigint primary key,
  status text not null default 'pending',
  updated_at timestamptz default now()
);
create table public.listing_reviews (
  id bigserial primary key,
  listing_id bigint not null,
  review_date date
);
-- task_jobs：库内 listing 的任务不出现在「我的任务」
create view public.task_jobs as
  select * from public.jobs j
  where j.etsy_listing_id is null
     or not exists (
          select 1 from public.product_library l
           where l.user_id = j.user_id and l.etsy_listing_id = j.etsy_listing_id);
