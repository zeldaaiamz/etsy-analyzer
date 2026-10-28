-- 056：产品库评论抓取「产品不可售」状态 + 手动录入不被批量重抓覆盖（Hongyan 2026-10-28）。未在生产执行；依赖 049 / 054。
-- 编号：036 / 050 预留，045 预留未用；055 为最新 → 本迁移 056。纯 create or replace / 约束放宽，不删表、不删数据。
-- 背景：反复「抓取失败 重试」的商品多数是 Etsy 已下架 / 售罄 / 店铺暂停（HTTP 410、"This item is unavailable"、
--   "Sorry, this item is sold out"、"This shop is taking a short break"）。重抓也不会成功 → 新状态 unavailable：
--   · worker 识别到后 finish_review_scrape(..., 'unavailable', error=原因)，claim 只领 pending → 不再自动重试
--   · 卡片显示红色「抓取失败：产品不可售」；用户仍可在卡片上点它显式重新排队（retry_review_scrape 接受 unavailable）
--   · 一键「手动抓取评论速度」（queue_library_review_scrapes）跳过 unavailable，也跳过本人已「手动录入」的（不被批量重抓覆盖）
-- 红线：status='ok' 永不重抓（除了获取失败，不重复获取）；不碰 sel_keyword_log；不碰 EHunt 队列；不写密钥。

-- 1) 状态增加 unavailable
alter table public.listing_scrape_status drop constraint if exists listing_scrape_status_status_check;
alter table public.listing_scrape_status add constraint listing_scrape_status_status_check
  check (status in ('ok', 'failed', 'pending', 'fetching', 'unavailable'));

-- 2) worker 写结果：新增 unavailable（与 failed 一样只改状态 + 原因，保留已有评论行）
create or replace function public.finish_review_scrape(p_listing bigint, p_status text, p_dates date[] default null,
                                                       p_source text default 'etsy_page', p_error text default null) returns void
language plpgsql security definer set search_path = public as $$
begin
  if p_status not in ('ok', 'failed', 'unavailable') then raise exception 'status 只能是 ok/failed/unavailable'; end if;
  if p_source not in ('etsy_page', 'etsy_api') then raise exception 'source 只能是 etsy_page/etsy_api'; end if;
  if p_status = 'ok' then
    delete from public.listing_reviews where listing_id = p_listing and source in ('etsy_page', 'etsy_api') and user_id is null;
    insert into public.listing_reviews (listing_id, review_date, source, review_key)
    select p_listing, d, p_source, d::text || '#' || row_number() over (partition by d order by d)
    from unnest(coalesce(p_dates, '{}')) as d
    where d between date '2005-01-01' and current_date + 1;
  end if;
  update public.listing_scrape_status set status = p_status, updated_at = now(),
    error = case when p_status = 'ok' then null
                 when p_status = 'unavailable' then left(coalesce(p_error, '产品不可售'), 500)
                 else left(coalesce(p_error, 'scrape failed'), 500) end
  where listing_id = p_listing;
end $$;
revoke all on function public.finish_review_scrape(bigint, text, date[], text, text) from public, anon, authenticated;
grant execute on function public.finish_review_scrape(bigint, text, date[], text, text) to service_role;

-- 3) 用户在卡片上显式重试：failed 或 unavailable → pending（仅本人产品库里的）
create or replace function public.retry_review_scrape(p_listing bigint) returns boolean
language plpgsql security definer set search_path = public as $$
begin
  if (select auth.uid()) is null then return false; end if;
  if not exists (select 1 from public.product_library where user_id = (select auth.uid()) and etsy_listing_id = p_listing) then return false; end if;
  update public.listing_scrape_status set status = 'pending', updated_at = now()
   where listing_id = p_listing and status in ('failed', 'unavailable');
  return found;
end $$;
revoke all on function public.retry_review_scrape(bigint) from public, anon;
grant execute on function public.retry_review_scrape(bigint) to authenticated;

-- 4) 一键「手动抓取评论速度」：与 054 相同，另外跳过 unavailable 和本人已手动录入（listing_review_manual）的
--    返回 jsonb 增加 skipped_unavailable / skipped_manual（旧前端忽略多出的键）
create or replace function public.queue_library_review_scrapes(p_listings bigint[] default null)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  uid uuid := (select auth.uid());
  scope bigint[];
  n_ins int := 0;
  n_fail int := 0;
  n_pend int := 0;
  n_ok int := 0;
  n_fetch int := 0;
  n_unav int := 0;
  n_man int := 0;
begin
  if uid is null then raise exception '需要登录'; end if;

  select coalesce(array_agg(distinct l.etsy_listing_id), '{}')
    into scope
    from public.product_library l
   where l.user_id = uid
     and l.etsy_listing_id is not null
     and (p_listings is null or coalesce(array_length(p_listings, 1), 0) = 0 or l.etsy_listing_id = any(p_listings));

  if coalesce(array_length(scope, 1), 0) = 0 then
    return jsonb_build_object('queued', 0, 'already_pending', 0, 'skipped_ok', 0, 'skipped_fetching', 0,
                              'skipped_unavailable', 0, 'skipped_manual', 0, 'total', 0);
  end if;

  -- 本人已手动录入、且状态不是 ok 的：不批量重抓（只能在卡片上显式「重新抓取」）
  select count(*) into n_man from unnest(scope) as s(lid)
   where exists (select 1 from public.listing_review_manual m where m.user_id = uid and m.listing_id = s.lid)
     and not exists (select 1 from public.listing_scrape_status x where x.listing_id = s.lid and x.status in ('ok', 'fetching', 'pending'));
  scope := array(select s.lid from unnest(scope) as s(lid)
                  where not exists (select 1 from public.listing_review_manual m where m.user_id = uid and m.listing_id = s.lid)
                     or exists (select 1 from public.listing_scrape_status x where x.listing_id = s.lid and x.status in ('ok', 'fetching', 'pending')));

  -- 缺失状态行 → pending（从未抓过）
  insert into public.listing_scrape_status (listing_id, status)
  select s.lid, 'pending'
    from unnest(scope) as s(lid)
   where not exists (select 1 from public.listing_scrape_status x where x.listing_id = s.lid)
  on conflict (listing_id) do nothing;
  get diagnostics n_ins = row_count;

  -- failed → pending（ok / fetching / pending / unavailable 不动）
  update public.listing_scrape_status
     set status = 'pending', updated_at = now(), error = null
   where listing_id = any(scope) and status = 'failed';
  get diagnostics n_fail = row_count;

  select count(*) into n_pend from public.listing_scrape_status where listing_id = any(scope) and status = 'pending';
  n_pend := greatest(0, n_pend - n_ins - n_fail);
  select count(*) into n_ok from public.listing_scrape_status where listing_id = any(scope) and status = 'ok';
  select count(*) into n_fetch from public.listing_scrape_status where listing_id = any(scope) and status = 'fetching';
  select count(*) into n_unav from public.listing_scrape_status where listing_id = any(scope) and status = 'unavailable';

  return jsonb_build_object(
    'queued', n_ins + n_fail,
    'already_pending', n_pend,
    'skipped_ok', n_ok,
    'skipped_fetching', n_fetch,
    'skipped_unavailable', n_unav,
    'skipped_manual', n_man,
    'total', n_ins + n_fail + n_pend);
end $$;
revoke all on function public.queue_library_review_scrapes(bigint[]) from public, anon;
grant execute on function public.queue_library_review_scrapes(bigint[]) to authenticated;

-- 5) 手动录入（019 save_manual_velocity 加校验；签名不变）：每段 0–10000 条、最近评论日期不晚于今天、不早于 10 年前
create or replace function public.save_manual_velocity(p_listing bigint, p_counts int[], p_latest date) returns boolean
language plpgsql security definer set search_path = public as $$
begin
  if (select auth.uid()) is null then return false; end if;
  if not exists (select 1 from public.product_library where user_id = (select auth.uid()) and etsy_listing_id = p_listing) then return false; end if;
  if coalesce(array_length(p_counts, 1), 0) <> 6 or exists (select 1 from unnest(p_counts) c where c is null or c < 0 or c > 10000) then
    raise exception '每月评论数需为 6 个 0–10000 的整数';
  end if;
  if p_latest is not null and p_latest > current_date then raise exception '最近评论日期不能晚于今天'; end if;
  if p_latest is not null and p_latest < current_date - 3650 then raise exception '最近评论日期太早'; end if;
  insert into public.listing_review_manual (user_id, listing_id, month_counts, latest_review_date)
  values ((select auth.uid()), p_listing, p_counts, p_latest)
  on conflict (user_id, listing_id) do update set month_counts = excluded.month_counts, latest_review_date = excluded.latest_review_date, updated_at = now();
  return true;
end $$;
revoke all on function public.save_manual_velocity(bigint, int[], date) from public, anon;
grant execute on function public.save_manual_velocity(bigint, int[], date) to authenticated;

-- 6) 回填：已有 failed 里证据明确的（HTTP 404/410、店铺暂停营业）→ unavailable。
--    「商品暂无评论区」不回填（可能是新品没评论，也可能是售罄/下架）：等下次显式重试时由新 worker 判断。
update public.listing_scrape_status
   set status = 'unavailable',
       error = case when error like '店铺暂停营业%' then '店铺暂停营业（This shop is taking a short break）'
                    else '商品已下架（HTTP ' || coalesce(substring(error from '(404|410)'), '404/410') || '）' end,
       updated_at = now()
 where status = 'failed'
   and (error ~ 'HTTP (404|410)$' or error like '%(404)%' or error like '店铺暂停营业%');
