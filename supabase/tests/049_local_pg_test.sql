-- 049 本地测试：新增产品自动 pending → worker 领取 → 写回；除了获取失败，不重复获取
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;
create or replace function pg_temp.st(l bigint) returns text language sql as $$ select status from public.listing_scrape_status where listing_id = l $$;
select pg_temp.ok(pg_temp.st(103) = 'pending' and pg_temp.st(104) = 'pending' and pg_temp.st(105) = 'pending', '回填：无状态的 103/104/105 → pending（105 只有截图评论也回填）');
select pg_temp.ok(pg_temp.st(101) = 'ok' and pg_temp.st(102) = 'failed', '回填不动已有 ok/failed');
-- 新增产品 → 触发器
insert into public.product_library (user_id, etsy_listing_id) values ('11111111-1111-1111-1111-111111111111', 201);
select pg_temp.ok(pg_temp.st(201) = 'pending', '新增产品 → pending');
insert into public.product_library (user_id, etsy_listing_id) values ('22222222-2222-2222-2222-222222222222', 101), ('22222222-2222-2222-2222-222222222222', 102);
select pg_temp.ok(pg_temp.st(101) = 'ok' and pg_temp.st(102) = 'failed', '已抓取/已失败的 listing 再次加入（另一用户）→ 不重复抓取（ON CONFLICT DO NOTHING）');
insert into public.product_library (user_id, etsy_listing_id) values ('11111111-1111-1111-1111-111111111111', null);
select pg_temp.ok(true, 'etsy_listing_id 为空也不报错');
-- 领取：小批量、只 pending
select array_agg(x order by x) as c from public.claim_review_scrapes(2) x \gset
select pg_temp.ok(cardinality(:'c'::bigint[]) = 2, '每轮最多领取 p_limit 个');
select pg_temp.ok((select count(*) from public.listing_scrape_status where status = 'fetching') = 2 and (select bool_and(attempts = 1) from public.listing_scrape_status where status = 'fetching'), '领取 → fetching，attempts+1');
select pg_temp.ok(not (101 = any(:'c'::bigint[])) and not (102 = any(:'c'::bigint[])), 'ok/failed 不会被领取');
select array_agg(x) as c2 from public.claim_review_scrapes(20) x \gset
select pg_temp.ok(cardinality(:'c2'::bigint[]) = 2 and not (:'c2'::bigint[] && :'c'::bigint[]), '第二轮领取剩余的 2 个，不与第一轮重复');
select pg_temp.ok((select count(*) from public.claim_review_scrapes(20)) = 0, '没有 pending 时领取为空');
-- 写回 ok：替换公共抓取行，保留截图/用户行
select public.finish_review_scrape(105, 'ok', array['2026-10-10', '2026-10-10', '2026-08-01']::date[], 'etsy_page', null);
select pg_temp.ok(pg_temp.st(105) = 'ok' and (select count(*) from public.listing_reviews where listing_id = 105 and source = 'etsy_page') = 3, 'ok：写入 3 个日期（同日 2 条不被唯一键吞掉）');
select pg_temp.ok((select count(*) from public.listing_reviews where listing_id = 105 and source = 'screenshot') = 1, 'ok：不动截图来源的评论');
select public.finish_review_scrape(105, 'ok', array['2026-10-11']::date[], 'etsy_api', null);
select pg_temp.ok((select count(*) from public.listing_reviews where listing_id = 105 and source in ('etsy_page', 'etsy_api')) = 1, '再次 ok：替换旧抓取行（不累加）');
select public.finish_review_scrape(105, 'ok', array['2099-01-01']::date[], 'etsy_page', null);
select pg_temp.ok((select count(*) from public.listing_reviews where listing_id = 105 and source = 'etsy_page') = 0, '未来日期被丢弃');
-- 写回 failed
select public.finish_review_scrape(104, 'failed', null, 'etsy_page', 'Etsy 反爬拦截（HTTP 403）');
select pg_temp.ok(pg_temp.st(104) = 'failed' and (select error from public.listing_scrape_status where listing_id = 104) like 'Etsy 反爬拦截%', 'failed：记录错误');
select pg_temp.ok((select count(*) from public.claim_review_scrapes(20)) = 0, 'failed 不会自动重抓');
-- 卡死回收
update public.listing_scrape_status set status = 'fetching', updated_at = now() - interval '16 minutes' where listing_id = 201;
select pg_temp.ok((select array_agg(x) from public.claim_review_scrapes(5) x) = array[201::bigint], '卡死 >15 分钟的 fetching 可重新领取');
-- 用户重试
select set_config('request.jwt.claim.sub', '22222222-2222-2222-2222-222222222222', false);
select pg_temp.ok(public.retry_review_scrape(104) = false and pg_temp.st(104) = 'failed', '别人产品库里的 listing 不能重试');
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', false);
select pg_temp.ok(public.retry_review_scrape(101) = false and pg_temp.st(101) = 'ok', '已成功的不能重试（不重复抓取）');
select pg_temp.ok(public.retry_review_scrape(104) = true and pg_temp.st(104) = 'pending', '失败的 → 重试 → pending');
select pg_temp.ok((select array_agg(x) from public.claim_review_scrapes(5) x) = array[104::bigint], '重试后被 worker 领取');
select set_config('request.jwt.claim.sub', '', false);
select pg_temp.ok(public.retry_review_scrape(104) = false, '未登录不能重试');
-- 权限
select pg_temp.ok(not has_function_privilege('authenticated', 'public.claim_review_scrapes(int)', 'execute') and not has_function_privilege('anon', 'public.finish_review_scrape(bigint,text,date[],text,text)', 'execute')
  and has_function_privilege('service_role', 'public.finish_review_scrape(bigint,text,date[],text,text)', 'execute') and has_function_privilege('authenticated', 'public.retry_review_scrape(bigint)', 'execute')
  and not has_function_privilege('anon', 'public.retry_review_scrape(bigint)', 'execute'), '权限：领取/写回仅 service_role，重试仅 authenticated');
do $$ begin begin perform public.finish_review_scrape(1, 'weird'); raise exception 'FAIL: bad status accepted'; exception when raise_exception then if sqlerrm like 'FAIL%' then raise; end if; end; raise notice 'ok: 非法 status 被拒绝'; end $$;
select 'ALL 049 TESTS PASSED';
