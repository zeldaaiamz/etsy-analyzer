-- 049 测试桩：与生产同结构的 product_library（相关列）/ listing_reviews / listing_scrape_status（019 原始约束）
create role anon; create role authenticated; create role service_role;
create schema auth; create table auth.users (id uuid primary key);
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
create table public.product_library (id uuid primary key default gen_random_uuid(), user_id uuid references auth.users(id), etsy_listing_id bigint, added_at timestamptz default now());
create table public.listing_reviews (id bigint generated always as identity primary key, listing_id bigint not null, review_date date not null, rating smallint,
  fetched_at timestamptz not null default now(), source text not null default 'etsy_page', review_key text, user_id uuid references auth.users(id), unique (listing_id, source, review_key));
create table public.listing_scrape_status (listing_id bigint primary key, status text not null default 'pending' check (status in ('ok', 'failed', 'pending')),
  error text, attempts int not null default 0, updated_at timestamptz not null default now());
-- 迁移前的现状：1 个已抓取（ok + 评论），1 个失败，2 个无状态（回填对象），1 个只有截图评论（也应回填）
insert into auth.users values ('11111111-1111-1111-1111-111111111111'), ('22222222-2222-2222-2222-222222222222');
insert into public.product_library (user_id, etsy_listing_id) values ('11111111-1111-1111-1111-111111111111', 101), ('11111111-1111-1111-1111-111111111111', 102),
  ('11111111-1111-1111-1111-111111111111', 103), ('22222222-2222-2222-2222-222222222222', 103), ('11111111-1111-1111-1111-111111111111', 104), ('11111111-1111-1111-1111-111111111111', 105);
insert into public.listing_reviews (listing_id, review_date, source, review_key) values (101, '2026-10-01', 'etsy_page', 'a'), (105, '2026-09-01', 'screenshot', 's');
insert into public.listing_scrape_status (listing_id, status) values (101, 'ok'), (102, 'failed');
