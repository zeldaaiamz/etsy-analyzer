-- 049：产品库新增商品自动抓取评论日期（评论速度）。纯追加式；未应用到生产。
-- 编号说明：048 为最新已上线；036 预留；045 已被 feat/ecs-selection 使用 → 本迁移用 049。
-- 流程：product_library INSERT → 触发器写 listing_scrape_status(pending)（ON CONFLICT DO NOTHING：已有 ok/failed 不动）
--      → box 上 review_scraper（独立进程，低优先级小批量）claim_review_scrapes → 抓取 → finish_review_scrape 写 listing_reviews + ok/failed
--      → 失败的由用户在产品库点「抓取失败 重试」→ retry_review_scrape 置回 pending。除了失败重试，不重复抓取。

-- 1) 状态增加 fetching（worker 已领取，超过 15 分钟未完成视为卡死可重新领取）
alter table public.listing_scrape_status drop constraint if exists listing_scrape_status_status_check;
alter table public.listing_scrape_status add constraint listing_scrape_status_status_check
  check (status in ('ok', 'failed', 'pending', 'fetching'));
create index if not exists listing_scrape_status_pending_idx on public.listing_scrape_status (updated_at) where status in ('pending', 'fetching');

-- 2) 新增产品 → pending
create or replace function public.enqueue_review_scrape() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.etsy_listing_id is not null then
    insert into public.listing_scrape_status (listing_id, status) values (new.etsy_listing_id, 'pending')
    on conflict (listing_id) do nothing;
  end if;
  return new;
end $$;
revoke all on function public.enqueue_review_scrape() from public, anon, authenticated;
drop trigger if exists product_library_enqueue_review_scrape on public.product_library;
create trigger product_library_enqueue_review_scrape after insert on public.product_library
  for each row execute function public.enqueue_review_scrape();

-- 3) 回填：产品库里还没有状态行、也没有抓取评论的 listing（2026-10-21 时约 30 个）
insert into public.listing_scrape_status (listing_id, status)
select distinct l.etsy_listing_id, 'pending' from public.product_library l
where l.etsy_listing_id is not null
  and not exists (select 1 from public.listing_reviews r where r.listing_id = l.etsy_listing_id and r.source in ('etsy_page', 'etsy_api'))
on conflict (listing_id) do nothing;

-- 4) worker 领取（仅 service_role）：pending 或卡死的 fetching，先进先出，SKIP LOCKED 支持多进程
create or replace function public.claim_review_scrapes(p_limit int default 3) returns setof bigint
language plpgsql security definer set search_path = public as $$
begin
  return query
  update public.listing_scrape_status s set status = 'fetching', attempts = s.attempts + 1, updated_at = now(), error = null
  where s.listing_id in (
    select listing_id from public.listing_scrape_status
    where status = 'pending' or (status = 'fetching' and updated_at < now() - interval '15 minutes')
    order by updated_at limit greatest(1, least(coalesce(p_limit, 3), 20))
    for update skip locked)
  returning s.listing_id;
end $$;
revoke all on function public.claim_review_scrapes(int) from public, anon, authenticated;
grant execute on function public.claim_review_scrapes(int) to service_role;

-- 5) worker 写结果（仅 service_role，单事务）：ok → 替换该来源的公共评论行；failed → 只改状态、保留已有评论
create or replace function public.finish_review_scrape(p_listing bigint, p_status text, p_dates date[] default null,
                                                       p_source text default 'etsy_page', p_error text default null) returns void
language plpgsql security definer set search_path = public as $$
begin
  if p_status not in ('ok', 'failed') then raise exception 'status 只能是 ok/failed'; end if;
  if p_source not in ('etsy_page', 'etsy_api') then raise exception 'source 只能是 etsy_page/etsy_api'; end if;
  if p_status = 'ok' then
    delete from public.listing_reviews where listing_id = p_listing and source in ('etsy_page', 'etsy_api') and user_id is null;
    insert into public.listing_reviews (listing_id, review_date, source, review_key)
    select p_listing, d, p_source, d::text || '#' || row_number() over (partition by d order by d)
    from unnest(coalesce(p_dates, '{}')) as d
    where d between date '2005-01-01' and current_date + 1;
  end if;
  update public.listing_scrape_status set status = p_status, updated_at = now(),
    error = case when p_status = 'ok' then null else left(coalesce(p_error, 'scrape failed'), 500) end
  where listing_id = p_listing;
end $$;
revoke all on function public.finish_review_scrape(bigint, text, date[], text, text) from public, anon, authenticated;
grant execute on function public.finish_review_scrape(bigint, text, date[], text, text) to service_role;

-- 6) 用户重试（仅自己产品库里的、且当前为 failed 的）
create or replace function public.retry_review_scrape(p_listing bigint) returns boolean
language plpgsql security definer set search_path = public as $$
begin
  if (select auth.uid()) is null then return false; end if;
  if not exists (select 1 from public.product_library where user_id = (select auth.uid()) and etsy_listing_id = p_listing) then return false; end if;
  update public.listing_scrape_status set status = 'pending', updated_at = now() where listing_id = p_listing and status = 'failed';
  return found;
end $$;
revoke all on function public.retry_review_scrape(bigint) from public, anon;
grant execute on function public.retry_review_scrape(bigint) to authenticated;
