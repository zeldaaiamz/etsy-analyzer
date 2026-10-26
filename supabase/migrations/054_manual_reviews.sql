-- 054：产品库「手动抓取评论速度」（Hongyan 2026-10-23）。未在生产执行；依赖 049。
-- 背景：049 AFTER INSERT 只在入库时写 pending；失败的不会自动再抓，只能逐条点「抓取失败 重试」。
--   入库一批后她想一键把缺数据的（failed / 无状态行）重新排队，点完可离开页面，等 reviews worker 慢慢抓。
-- RPC：queue_library_review_scrapes(p_listings)
--   · p_listings 为空/null → 本人产品库里所有「需要数据」的 listing
--   · p_listings 有值 → 只处理选中的（仍须属于本人产品库）
--   · 需要数据 = 无 listing_scrape_status 行，或 status ∈ ('failed','pending')
--   · 红线：status='ok' 永不重抓（除了获取失败，不重复获取）；fetching 不动（已在抓）
--   · 不碰 sel_keyword_log；不碰 EHunt 队列；不写密钥
-- 返回 jsonb：{ queued, already_pending, skipped_ok, skipped_fetching, total }
--   queued = 新置 pending（含 insert 缺失行 + failed→pending）
--   already_pending = 本来就是 pending 的
--   total = queued + already_pending（toast「已排队 N 个」用这个）

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
begin
  if uid is null then raise exception '需要登录'; end if;

  if p_listings is not null and coalesce(array_length(p_listings, 1), 0) > 0 then
    select coalesce(array_agg(distinct l.etsy_listing_id), '{}')
      into scope
      from public.product_library l
     where l.user_id = uid
       and l.etsy_listing_id is not null
       and l.etsy_listing_id = any(p_listings);
  else
    select coalesce(array_agg(distinct l.etsy_listing_id), '{}')
      into scope
      from public.product_library l
     where l.user_id = uid
       and l.etsy_listing_id is not null;
  end if;

  if coalesce(array_length(scope, 1), 0) = 0 then
    return jsonb_build_object(
      'queued', 0, 'already_pending', 0, 'skipped_ok', 0, 'skipped_fetching', 0, 'total', 0);
  end if;

  -- 缺失状态行 → pending（从未抓过）
  insert into public.listing_scrape_status (listing_id, status)
  select s.lid, 'pending'
    from unnest(scope) as s(lid)
   where not exists (select 1 from public.listing_scrape_status x where x.listing_id = s.lid)
  on conflict (listing_id) do nothing;
  get diagnostics n_ins = row_count;

  -- failed → pending（显式重试；ok / fetching / pending 不动）
  update public.listing_scrape_status
     set status = 'pending', updated_at = now(), error = null
   where listing_id = any(scope) and status = 'failed';
  get diagnostics n_fail = row_count;

  select count(*) into n_pend from public.listing_scrape_status
   where listing_id = any(scope) and status = 'pending';
  -- n_pend 含刚插入/刚从 failed 改过来的；already_pending = 改之前就是 pending 的
  n_pend := greatest(0, n_pend - n_ins - n_fail);

  select count(*) into n_ok from public.listing_scrape_status
   where listing_id = any(scope) and status = 'ok';
  select count(*) into n_fetch from public.listing_scrape_status
   where listing_id = any(scope) and status = 'fetching';

  return jsonb_build_object(
    'queued', n_ins + n_fail,
    'already_pending', n_pend,
    'skipped_ok', n_ok,
    'skipped_fetching', n_fetch,
    'total', n_ins + n_fail + n_pend);
end $$;

revoke all on function public.queue_library_review_scrapes(bigint[]) from public, anon;
grant execute on function public.queue_library_review_scrapes(bigint[]) to authenticated;
