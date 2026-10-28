-- 056 测试桩（在 049_stub + 049 + 054 之后）：019 的 listing_review_manual + 原版 save_manual_velocity
create table if not exists public.listing_review_manual (
  user_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  listing_id bigint not null,
  month_counts int[] not null check (array_length(month_counts, 1) = 6 and 0 <= all(month_counts)),
  latest_review_date date,
  source text not null default 'manual' check (source = 'manual'),
  updated_at timestamptz not null default now(),
  primary key (user_id, listing_id)
);
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
-- 迁移前的生产形态：失败原因各异（410 / 404 / 店铺暂停 / 暂无评论区 / 反爬）
insert into public.product_library (user_id, etsy_listing_id) values
  ('11111111-1111-1111-1111-111111111111', 401), ('11111111-1111-1111-1111-111111111111', 402), ('11111111-1111-1111-1111-111111111111', 403),
  ('11111111-1111-1111-1111-111111111111', 404), ('11111111-1111-1111-1111-111111111111', 405), ('11111111-1111-1111-1111-111111111111', 406);
update public.listing_scrape_status set status = 'failed', error = e from (values
  (401::bigint, 'Etsy 商品页 HTTP 410'), (402, 'Etsy 商品不存在或已下架 (404)'), (403, '店铺暂停营业（页面不显示评论）'),
  (404, '商品暂无评论区'), (405, 'Etsy 反爬拦截（HTTP 403）'), (406, 'Etsy 商品页 HTTP 500')) v(l, e)
 where listing_id = v.l;
