-- 019：评论日期（按评论速度排序用）。纯追加式，未应用；旧前端/worker 不受影响。
-- 数据来源：真实浏览器抓取 Etsy 商品页评论（source='etsy_page'）；以后也可写入 'etsy_api'（v3 getReviewsByListing.create_timestamp）。
create table if not exists public.listing_reviews (
  id bigint generated always as identity primary key,
  listing_id bigint not null,
  review_date date not null,
  rating smallint check (rating between 1 and 5),
  fetched_at timestamptz not null default now(),
  source text not null default 'etsy_page',
  review_key text,               -- 抓取端去重键（如评论 id 或 hash），可空
  unique (listing_id, source, review_key)
);
create index if not exists listing_reviews_listing_date_idx on public.listing_reviews (listing_id, review_date desc);
alter table public.listing_reviews enable row level security;
-- 评论是公开数据：已登录用户可读；仅 service_role（抓取端/worker）可写
create policy listing_reviews_select on public.listing_reviews for select to authenticated using (true);
revoke insert, update, delete on public.listing_reviews from anon, authenticated;
revoke all on public.listing_reviews from anon;

-- ---------- 抓取状态（每个 listing 一行；抓取端用 service_role 写入）----------
create table if not exists public.listing_scrape_status (
  listing_id bigint primary key,
  status text not null default 'pending' check (status in ('ok', 'failed', 'pending')),
  error text,
  attempts int not null default 0,
  updated_at timestamptz not null default now()
);
alter table public.listing_scrape_status enable row level security;
create policy listing_scrape_status_select on public.listing_scrape_status for select to authenticated using (true);
revoke insert, update, delete on public.listing_scrape_status from anon, authenticated;
revoke all on public.listing_scrape_status from anon;

-- ---------- 手动填写的评论速度（抓取失败时；source='manual'，按用户隔离）----------
-- month_counts[1] = 最近 30 天，[2] = 30-60 天 … [6] = 150-180 天
create table if not exists public.listing_review_manual (
  user_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  listing_id bigint not null,
  month_counts int[] not null check (array_length(month_counts, 1) = 6 and 0 <= all(month_counts)),
  latest_review_date date,
  source text not null default 'manual' check (source = 'manual'),
  updated_at timestamptz not null default now(),
  primary key (user_id, listing_id)
);
alter table public.listing_review_manual enable row level security;
create policy listing_review_manual_select on public.listing_review_manual for select to authenticated using (user_id = (select auth.uid()));
revoke insert, update, delete on public.listing_review_manual from anon, authenticated;
revoke all on public.listing_review_manual from anon;

create or replace function public.save_manual_velocity(p_listing bigint, p_counts int[], p_latest date) returns boolean
language plpgsql security definer set search_path = public as $$
begin
  if (select auth.uid()) is null then return false; end if;
  if not exists (select 1 from public.product_library where user_id = (select auth.uid()) and etsy_listing_id = p_listing) then return false; end if;
  if p_latest is not null and p_latest > current_date then raise exception '最近评论日期不能晚于今天'; end if;
  insert into public.listing_review_manual (user_id, listing_id, month_counts, latest_review_date)
  values ((select auth.uid()), p_listing, p_counts, p_latest)
  on conflict (user_id, listing_id) do update set month_counts = excluded.month_counts, latest_review_date = excluded.latest_review_date, updated_at = now();
  return true;
end $$;
revoke all on function public.save_manual_velocity(bigint, int[], date) from public, anon;
grant execute on function public.save_manual_velocity(bigint, int[], date) to authenticated;

-- ---------- 评论截图 OCR（抓取失败时的主要兜底；本地 tesseract，不调外部视觉 API）----------
-- 截图存私有 bucket，路径 <user_id>/<listing_id>/<file>；仅本人可读写
insert into storage.buckets (id, name, public) values ('review-shots', 'review-shots', false) on conflict (id) do nothing;
create policy review_shots_owner_select on storage.objects for select to authenticated
  using (bucket_id = 'review-shots' and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy review_shots_owner_insert on storage.objects for insert to authenticated
  with check (bucket_id = 'review-shots' and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy review_shots_owner_delete on storage.objects for delete to authenticated
  using (bucket_id = 'review-shots' and (storage.foldername(name))[1] = (select auth.uid())::text);

-- 截图来源的评论按用户隔离（etsy_page 行 user_id 为空 = 公共）
alter table public.listing_reviews add column if not exists user_id uuid references auth.users(id) on delete cascade;
drop policy if exists listing_reviews_select on public.listing_reviews;
create policy listing_reviews_select on public.listing_reviews for select to authenticated using (user_id is null or user_id = (select auth.uid()));

create table if not exists public.review_ocr_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  listing_id bigint not null,
  image_paths text[] not null,
  status text not null default 'queued' check (status in ('queued', 'processing', 'done', 'failed', 'confirmed')),
  extracted_dates date[],          -- worker 识别（去重、新→旧）
  total_reviews int,               -- 识别到的评论总数（如 "Reviews for this item (1,234)"）
  ocr_error text,
  confirmed_dates date[],          -- 用户核对/修改后的日期，确认后写入 listing_reviews(source='screenshot')
  created_at timestamptz not null default now(),
  processed_at timestamptz, confirmed_at timestamptz
);
create index if not exists review_ocr_jobs_queue_idx on public.review_ocr_jobs (status, created_at) where status = 'queued';
alter table public.review_ocr_jobs enable row level security;
create policy review_ocr_jobs_select on public.review_ocr_jobs for select to authenticated using (user_id = (select auth.uid()));
revoke insert, update, delete on public.review_ocr_jobs from anon, authenticated;
revoke all on public.review_ocr_jobs from anon;

create or replace function public.queue_review_ocr(p_listing bigint, p_paths text[]) returns uuid
language plpgsql security definer set search_path = public as $$
declare u uuid := (select auth.uid()); r uuid;
begin
  if u is null or coalesce(array_length(p_paths, 1), 0) = 0 or array_length(p_paths, 1) > 20 then return null; end if;
  if not exists (select 1 from public.product_library where user_id = u and etsy_listing_id = p_listing) then return null; end if;
  if exists (select 1 from unnest(p_paths) p where p not like u::text || '/' || p_listing || '/%') then raise exception '截图路径无效'; end if;
  insert into public.review_ocr_jobs (user_id, listing_id, image_paths) values (u, p_listing, p_paths) returning id into r;
  return r;
end $$;

-- worker（service_role）领取一条
create or replace function public.claim_review_ocr() returns setof public.review_ocr_jobs
language sql security definer set search_path = public as $$
  update public.review_ocr_jobs set status = 'processing' where id = (
    select id from public.review_ocr_jobs where status = 'queued' order by created_at for update skip locked limit 1) returning *;
$$;

-- 用户核对后确认：替换该用户该 listing 之前的截图日期
create or replace function public.confirm_review_dates(p_job uuid, p_dates date[]) returns int
language plpgsql security definer set search_path = public as $$
declare u uuid := (select auth.uid()); j public.review_ocr_jobs; n int;
begin
  select * into j from public.review_ocr_jobs where id = p_job and user_id = u and status in ('done', 'confirmed');
  if not found then return 0; end if;
  if exists (select 1 from unnest(p_dates) d where d > current_date or d < current_date - 3650) then raise exception '日期超出范围'; end if;
  delete from public.listing_reviews where user_id = u and listing_id = j.listing_id and source = 'screenshot';
  insert into public.listing_reviews (user_id, listing_id, review_date, source, review_key)
    select u, j.listing_id, d, 'screenshot', d::text || '#' || row_number() over (partition by d) from unnest(p_dates) d;
  get diagnostics n = row_count;
  update public.review_ocr_jobs set status = 'confirmed', confirmed_dates = p_dates, confirmed_at = now() where id = p_job;
  return n;
end $$;
revoke all on function public.queue_review_ocr(bigint, text[]), public.confirm_review_dates(uuid, date[]), public.claim_review_ocr() from public, anon;
grant execute on function public.queue_review_ocr(bigint, text[]), public.confirm_review_dates(uuid, date[]) to authenticated;
grant execute on function public.claim_review_ocr() to service_role;
