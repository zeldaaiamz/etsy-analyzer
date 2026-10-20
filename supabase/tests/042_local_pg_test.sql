-- 042 本地测试：除了获取失败，不重复获取
\set u1 '11111111-1111-1111-1111-111111111111'
insert into auth.users values (:'u1');
create or replace function pg_temp.as_user(u uuid) returns void language sql as $$ select set_config('request.jwt.claim.sub', u::text, false) $$;
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;
insert into public.sel_runs (id, user_id, keyword, status) values ('aaaaaaaa-0000-0000-0000-000000000001', :'u1', 'k', 'succeeded'), ('aaaaaaaa-0000-0000-0000-000000000002', :'u1', 'k2', 'succeeded');
insert into public.sel_batches (id, run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end, status) values
 ('bbbbbbbb-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001', :'u1', 1, 1, 10, 1, 1, 'done'),
 ('bbbbbbbb-0000-0000-0000-000000000002', 'aaaaaaaa-0000-0000-0000-000000000002', :'u1', 2, 11, 20, 2, 2, 'done');
insert into public.sel_items (run_id, batch_id, user_id, batch_no, etsy_listing_id, rank, verdict)
 select 'aaaaaaaa-0000-0000-0000-000000000001', 'bbbbbbbb-0000-0000-0000-000000000001', :'u1', 1, 1000 + g, g, '保留' from generate_series(1, 10) g;
-- 第 2 批：5 条与第 1 批重复 + 5 条新
insert into public.sel_items (run_id, batch_id, user_id, batch_no, etsy_listing_id, rank, verdict)
 select 'aaaaaaaa-0000-0000-0000-000000000002', 'bbbbbbbb-0000-0000-0000-000000000002', :'u1', 2, case when g <= 5 then 1000 + g else 2000 + g end, 100 + g, '保留' from generate_series(1, 10) g;
select pg_temp.as_user(:'u1');
select pg_temp.ok((public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001'::uuid]))->>'sent' = '10', '首次发送 10 条');
-- 模拟结果：3 条成功、2 条失败、2 条取消、其余排队/处理中
update public.jobs set status = 'succeeded' where etsy_listing_id in (1001, 1002, 1003);
update public.jobs set status = 'failed' where etsy_listing_id in (1004, 1005);
update public.jobs set status = 'canceled' where etsy_listing_id in (1006, 1007);
update public.jobs set status = 'running' where etsy_listing_id = 1008;
select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001'::uuid], true) as r \gset
select pg_temp.ok((:'r'::jsonb)->>'sent' = '4' and (:'r'::jsonb)->>'reused' = '6', '再发一次：只重新获取失败/取消的 4 条，6 条（成功/排队/处理中）复用');
select pg_temp.ok((select count(*) from public.jobs where etsy_listing_id = 1001) = 1, '成功的 listing 没有第二个任务');
select pg_temp.ok((select count(*) from public.jobs where etsy_listing_id = 1008) = 1, '处理中的 listing 没有第二个任务');
select pg_temp.ok((select count(*) from public.jobs where etsy_listing_id = 1004) = 2, '失败的 listing 重新获取');
select pg_temp.ok((public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001'::uuid], true))->>'sent' = '0', '再发第三次：全部已有非失败任务，0 条新建');
select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000002'::uuid]) as r \gset
select pg_temp.ok((:'r'::jsonb)->>'sent' = '5' and (:'r'::jsonb)->>'reused' = '5', '跨批次重复的 5 条复用，只发 5 条新');
select pg_temp.ok((select sent_count from public.sel_batches where batch_no = 2) = 10, '复用计入 sent_count（不会显示“发送剩余”）');
-- EHunt 主图查询
reset role;
insert into public.ehunt_lookup_requests (listing_id, status, image_url, attempts, finished_at) values
 (1, 'done', 'https://i.etsystatic.com/a.jpg', 1, now() - interval '30 days'),
 (2, 'error', null, 1, now()), (3, 'not_found', null, 1, now() - interval '2 hours'), (4, 'not_found', null, 1, now() - interval '2 days'),
 (5, 'login_expired', null, 1, now());
insert into public.ehunt_lookup_requests (listing_id, status, attempts, claimed_at) values (6, 'running', 1, now() - interval '10 minutes'), (7, 'running', 1, now());
select pg_temp.ok((public.ehunt_lookup_request(1)).status = 'done' and (select image_url from public.ehunt_lookup_requests where listing_id = 1) is not null, '成功结果 30 天后也不重新获取');
select pg_temp.ok((public.ehunt_lookup_request(2)).status = 'queued', '失败(error) 重新排队');
select pg_temp.ok((public.ehunt_lookup_request(3)).status = 'not_found', 'not_found 24h 内复用');
select pg_temp.ok((public.ehunt_lookup_request(4)).status = 'queued', 'not_found 超过 24h 可重试');
select pg_temp.ok((public.ehunt_lookup_request(5)).status = 'queued', 'login_expired 重新排队');
select pg_temp.ok((public.ehunt_lookup_request(7)).status = 'running', '处理中原样返回');
select pg_temp.ok(not exists (select 1 from public.ehunt_lookup_claim('w', 50) where listing_id in (1, 6, 7)), 'claim 不再认领成功/处理中的项');
select pg_temp.ok((select status = 'error' from public.ehunt_lookup_requests where listing_id = 6), '处理超时 → 记为获取失败');
select pg_temp.ok((public.ehunt_lookup_request(6)).status = 'queued', '超时失败后才可重新排队');
update public.ehunt_lookup_requests set status = 'error', finished_at = now() - interval '10 days' where listing_id = 2;
select public.ehunt_lookup_purge();
select pg_temp.ok(exists (select 1 from public.ehunt_lookup_requests where listing_id = 1) and not exists (select 1 from public.ehunt_lookup_requests where listing_id = 2), 'purge 保留成功结果、只清失败');
select 'ALL 042 TESTS PASSED';
