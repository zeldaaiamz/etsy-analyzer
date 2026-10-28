-- 056 本地测试：unavailable 状态；回填；worker 写 unavailable；重试；批量排队跳过 unavailable / 手动录入；手动录入校验
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;
create or replace function pg_temp.st(l bigint) returns text language sql as $$ select status from public.listing_scrape_status where listing_id = l $$;
create or replace function pg_temp.er(l bigint) returns text language sql as $$ select error from public.listing_scrape_status where listing_id = l $$;
create or replace function pg_temp.j(k text) returns int language sql as $$ select (current_setting('pg_temp.last_q', true)::jsonb ->> k)::int $$;

-- 1) 回填：证据明确的才改
select pg_temp.ok(pg_temp.st(401) = 'unavailable' and pg_temp.er(401) = '商品已下架（HTTP 410）', '回填：HTTP 410 → unavailable');
select pg_temp.ok(pg_temp.st(402) = 'unavailable' and pg_temp.er(402) = '商品已下架（HTTP 404）', '回填：(404) → unavailable');
select pg_temp.ok(pg_temp.st(403) = 'unavailable' and pg_temp.er(403) like '店铺暂停营业%', '回填：店铺暂停营业 → unavailable');
select pg_temp.ok(pg_temp.st(404) = 'failed' and pg_temp.st(405) = 'failed' and pg_temp.st(406) = 'failed', '回填：暂无评论区 / 反爬 / HTTP 500 仍 failed');
select pg_temp.ok(pg_temp.st(101) = 'ok', '回填：ok 不动');

-- 2) worker（service_role）写 unavailable；claim 不再领取
select public.finish_review_scrape(406, 'unavailable', null, 'etsy_page', '商品已售罄（Sorry, this item is sold out）');
select pg_temp.ok(pg_temp.st(406) = 'unavailable' and pg_temp.er(406) like '商品已售罄%', 'finish：写 unavailable + 原因');
select public.finish_review_scrape(405, 'unavailable', null, 'etsy_page', null);
select pg_temp.ok(pg_temp.er(405) = '产品不可售', 'finish：unavailable 无原因 → 产品不可售');
update public.listing_scrape_status set status = 'pending' where listing_id = 405;
select pg_temp.ok(not exists (select 1 from public.claim_review_scrapes(50) c where c in (401, 402, 403, 406)), 'claim：unavailable 永不自动领取');
select public.finish_review_scrape(405, 'failed', null, 'etsy_page', 'Etsy 反爬拦截（HTTP 403）');
do $$ begin
  begin perform public.finish_review_scrape(405, 'bogus'); raise exception 'FAIL: bogus 应报错';
  exception when raise_exception then if sqlerrm like 'FAIL%' then raise; end if; end;
  raise notice 'ok: finish 拒绝未知状态';
end $$;
select pg_temp.ok(has_function_privilege('service_role', 'public.finish_review_scrape(bigint, text, date[], text, text)', 'execute')
  and not has_function_privilege('authenticated', 'public.finish_review_scrape(bigint, text, date[], text, text)', 'execute'), 'finish：仅 service_role');

-- 3) 批量排队：跳过 unavailable；跳过本人手动录入；ok 永不重抓
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', false);
select pg_temp.ok(public.save_manual_velocity(404, array[3,2,1,0,0,0], current_date - 3), '手动录入 404（在售但抓不到）');
select set_config('pg_temp.last_q', public.queue_library_review_scrapes(null)::text, false);
select pg_temp.ok(pg_temp.st(401) = 'unavailable' and pg_temp.st(402) = 'unavailable' and pg_temp.st(403) = 'unavailable' and pg_temp.st(406) = 'unavailable', '批量：unavailable 不排队');
select pg_temp.ok(pg_temp.j('skipped_unavailable') = 4, '批量：skipped_unavailable = 4');
select pg_temp.ok(pg_temp.st(404) = 'failed' and pg_temp.j('skipped_manual') = 1, '批量：已手动录入的 failed 不重抓（skipped_manual=1）');
select pg_temp.ok(pg_temp.st(405) = 'pending', '批量：普通 failed → pending');
select pg_temp.ok(pg_temp.st(101) = 'ok' and pg_temp.j('skipped_ok') >= 1, '批量：ok 不重抓（除了获取失败，不重复获取）');
select set_config('pg_temp.last_q', public.queue_library_review_scrapes(array[401, 404]::bigint[])::text, false);
select pg_temp.ok(pg_temp.j('queued') = 0 and pg_temp.st(401) = 'unavailable' and pg_temp.st(404) = 'failed', '选中：unavailable / 手动 也不排队');

-- 4) 卡片上显式重试：unavailable / failed（含已手动的）→ pending
select pg_temp.ok(public.retry_review_scrape(401) and pg_temp.st(401) = 'pending', '显式重试：unavailable → pending');
select pg_temp.ok(public.retry_review_scrape(404) and pg_temp.st(404) = 'pending', '显式重试：手动录入过的 failed 也可重抓');
select pg_temp.ok(not public.retry_review_scrape(101) and pg_temp.st(101) = 'ok', '显式重试：ok 不动');
select set_config('request.jwt.claim.sub', '22222222-2222-2222-2222-222222222222', false);
select pg_temp.ok(not public.retry_review_scrape(402) and pg_temp.st(402) = 'unavailable', '显式重试：别人的不动');

-- 5) 手动录入校验
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', false);
do $$ declare bad record; begin
  for bad in select * from (values (array[1,2,3]::int[], current_date, '6 个'), (array[1,-1,0,0,0,0], current_date, '0–10000'),
                                   (array[1,0,0,0,0,0], current_date + 1, '晚于今天'), (array[1,0,0,0,0,0], current_date - 4000, '太早')) v(c, d, m) loop
    begin perform public.save_manual_velocity(405, bad.c, bad.d); raise exception 'FAIL: 应拒绝 %', bad.m;
    exception when raise_exception then if sqlerrm like 'FAIL%' then raise; end if;
      if sqlerrm not like '%' || bad.m || '%' then raise exception 'FAIL: 文案不对 % / %', bad.m, sqlerrm; end if; end;
  end loop;
  raise notice 'ok: 手动录入校验';
end $$;
select pg_temp.ok(not public.save_manual_velocity(999, array[1,0,0,0,0,0], current_date), '手动录入：不在库里的拒绝');
select pg_temp.ok(public.save_manual_velocity(404, array[5,0,0,0,0,0], current_date), '手动录入：再次保存');
select pg_temp.ok((select month_counts[1] from public.listing_review_manual where listing_id = 404) = 5, '手动录入：覆盖更新');

-- 6) 约束 + 不碰 sel_keyword_log
select pg_temp.ok((select pg_get_constraintdef(oid) from pg_constraint where conname = 'listing_scrape_status_status_check') like '%unavailable%', '约束含 unavailable');
create table if not exists public.sel_keyword_log (id bigserial primary key, keyword text not null);
insert into public.sel_keyword_log (keyword) values ('gift');
select set_config('pg_temp.kw', (select count(*)::text from public.sel_keyword_log), false);
select public.queue_library_review_scrapes(null);
select pg_temp.ok((select count(*) from public.sel_keyword_log) = current_setting('pg_temp.kw')::int, '不碰 sel_keyword_log');
select 'ALL 056 TESTS PASSED';
