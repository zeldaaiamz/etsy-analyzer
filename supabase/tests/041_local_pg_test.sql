-- 041 本地测试（stub + 024…039 之后，先造 039 时代的重复数据，再执行 041 清理）
\set u1 '11111111-1111-1111-1111-111111111111'
\set u2 '22222222-2222-2222-2222-222222222222'
insert into auth.users values (:'u1'), (:'u2');
create or replace function pg_temp.as_user(u uuid) returns void language sql as $$ select set_config('request.jwt.claim.sub', u::text, false) $$;
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;
-- 造 039 前的重复：u1 两个旧批次各 10 条（listing 1..10 / 6..15 → 重叠 5 条），重叠里 listing 6 第一份已排除、listing 7 第二份已有数据，listing 8 第二份隐藏
insert into public.batches (id, user_id, name) values ('cccccccc-0000-0000-0000-000000000001', :'u1', '获取链接 2026-10-20T01:10:19.138Z'), ('cccccccc-0000-0000-0000-000000000002', :'u1', '获取链接 2026-10-20T01:11:51.821Z');
insert into public.jobs (batch_id, user_id, etsy_url, etsy_listing_id, prescreen, created_at) select 'cccccccc-0000-0000-0000-000000000001', :'u1', 'https://www.etsy.com/listing/' || g || '/x', g, true, now() - interval '2 min' from generate_series(1, 10) g;
insert into public.jobs (batch_id, user_id, etsy_url, etsy_listing_id, prescreen, created_at) select 'cccccccc-0000-0000-0000-000000000002', :'u1', 'https://de.etsy.com/listing/' || g || '/?ref=x', g, true, now() - interval '1 min' from generate_series(6, 15) g;
update public.jobs set status = 'canceled', screen_state = 'rejected' where etsy_listing_id = 6 and batch_id = 'cccccccc-0000-0000-0000-000000000001';
update public.jobs set status = 'screening', screen_state = 'awaiting' where etsy_listing_id = 7 and batch_id = 'cccccccc-0000-0000-0000-000000000002';
update public.jobs set screen_hidden_at = now() where etsy_listing_id = 8 and batch_id = 'cccccccc-0000-0000-0000-000000000002';
-- u2 同一 listing 1：不同用户互不影响
insert into public.batches (id, user_id, name) values ('cccccccc-0000-0000-0000-000000000009', :'u2', 'x');
insert into public.jobs (batch_id, user_id, etsy_url, etsy_listing_id, prescreen) values ('cccccccc-0000-0000-0000-000000000009', :'u2', 'https://www.etsy.com/listing/1/x', 1, true);
select pg_temp.ok((select count(*) - count(distinct (user_id, etsy_listing_id)) from public.jobs where prescreen and screen_hidden_at is null) = 4, '迁移前：列表里重复 4 条');
\i migrations/041_prescreen_dedupe.sql
select pg_temp.ok((select count(*) - count(distinct (user_id, etsy_listing_id)) from public.jobs where prescreen and screen_hidden_at is null) = 0, '清理后：列表里没有重复');
select pg_temp.ok((select count(*) from public.jobs) = 21, '清理不删除任何行（只隐藏）');
select pg_temp.ok((select count(*) from public.jobs where screen_hidden_at is not null) = 5, '隐藏 4 条重复 + 原本隐藏 1 条');
select pg_temp.ok((select batch_id = 'cccccccc-0000-0000-0000-000000000001' from public.jobs where etsy_listing_id = 6 and screen_hidden_at is null), '保留已排除的那份');
select pg_temp.ok((select batch_id = 'cccccccc-0000-0000-0000-000000000002' from public.jobs where etsy_listing_id = 7 and screen_hidden_at is null), '保留已有数据的那份（即使较晚）');
select pg_temp.ok((select bool_and(status = 'canceled') from public.jobs where screen_hidden_at is not null and etsy_listing_id in (9, 10)), '被隐藏的排队重复改为取消（不再耗额度）');
select pg_temp.ok((select count(*) from public.jobs where user_id = :'u2' and screen_hidden_at is null) = 1, '其他用户不受影响');
-- 触发器：旧网页路径直接 insert（URL 变体、无 listing id）→ 已在列表里的跳过，新的进来，不报错
select pg_temp.as_user(:'u1');
insert into public.jobs (batch_id, etsy_url, prescreen) values ('cccccccc-0000-0000-0000-000000000001', 'https://www.etsy.com/listing/3/abc?click_key=1#r', true), ('cccccccc-0000-0000-0000-000000000001', 'https://etsy.com/listing/99/', true), ('cccccccc-0000-0000-0000-000000000001', 'https://etsy.com/listing/99/y', true);
select pg_temp.ok((select count(*) from public.jobs where etsy_listing_id = 3 and screen_hidden_at is null) = 1, '旧路径 insert：URL 变体的重复被跳过');
select pg_temp.ok((select count(*) from public.jobs where etsy_listing_id = 99) = 1, '同一条 insert 里重复的也只进 1 条，并补上 etsy_listing_id');
insert into public.jobs (batch_id, etsy_url, prescreen) values ('cccccccc-0000-0000-0000-000000000001', 'https://www.etsy.com/listing/3/x', false);
select pg_temp.ok((select count(*) from public.jobs where etsy_url = 'https://www.etsy.com/listing/3/x' and not prescreen) = 1, '非预筛选任务（强制重跑）不受影响');
insert into public.jobs (batch_id, etsy_url, etsy_listing_id, prescreen) values ('cccccccc-0000-0000-0000-000000000001', 'https://www.etsy.com/listing/8/x', 8, true);
select pg_temp.ok((select count(*) from public.jobs where etsy_listing_id = 8 and screen_hidden_at is null) = 1, '列表里已有 8（另一份隐藏）→ 跳过');
-- sel_send_to_screen：跨批次 / 重发
insert into public.sel_runs (id, user_id, keyword, status) values ('aaaaaaaa-0000-0000-0000-000000000001', :'u1', 'acrylic sign', 'succeeded'), ('aaaaaaaa-0000-0000-0000-000000000002', :'u1', 'sign', 'succeeded');
insert into public.sel_batches (id, run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end, status) values
 ('bbbbbbbb-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001', :'u1', 1, 1, 1000, 1, 50, 'done'),
 ('bbbbbbbb-0000-0000-0000-000000000002', 'aaaaaaaa-0000-0000-0000-000000000002', :'u1', 2, 1001, 2000, 51, 100, 'done'),
 ('bbbbbbbb-0000-0000-0000-000000000003', 'aaaaaaaa-0000-0000-0000-000000000001', :'u1', 3, 2001, 3000, 101, 150, 'done');
insert into public.sel_items (run_id, batch_id, user_id, batch_no, etsy_listing_id, rank, verdict) select 'aaaaaaaa-0000-0000-0000-000000000001', 'bbbbbbbb-0000-0000-0000-000000000001', :'u1', 1, g, g, '保留' from generate_series(11, 40) g;   -- 11..15 已在列表
insert into public.sel_items (run_id, batch_id, user_id, batch_no, etsy_listing_id, rank, verdict) select 'aaaaaaaa-0000-0000-0000-000000000002', 'bbbbbbbb-0000-0000-0000-000000000002', :'u1', 2, g, 100 + g, '保留' from generate_series(31, 50) g;  -- 31..40 与另一个任务的第 1 批重叠
insert into public.sel_items (run_id, batch_id, user_id, batch_no, etsy_listing_id, rank, verdict) select 'aaaaaaaa-0000-0000-0000-000000000001', 'bbbbbbbb-0000-0000-0000-000000000003', :'u1', 3, g, 200 + g, '保留' from generate_series(1, 5) g;    -- 全部已在列表
select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001'::uuid]) as r \gset
select pg_temp.ok((:'r'::jsonb)->>'sent' = '25' and (:'r'::jsonb)->>'dup' = '5', '第 1 批 30 条：5 条已在列表 → 只发 25');
select pg_temp.ok((select sent_count from public.sel_batches where batch_no = 1) = 30, '重复的算本批已发送（30/30）');
select pg_temp.ok((select name from public.batches order by created_at desc, name limit 1) like '获取链接 acrylic sign 第1批 %', '批次命名保持 「<keyword> 第N批」');
select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000002'::uuid]) as r \gset
select pg_temp.ok((:'r'::jsonb)->>'sent' = '10' and (:'r'::jsonb)->>'dup' = '10', '另一次调用发另一个任务的批次：与另一个任务的第 1 批重叠的 10 条跳过');
select count(*) as nb0 from public.batches \gset
select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000003'::uuid]) as r \gset
select pg_temp.ok((:'r'::jsonb)->>'sent' = '0' and (:'r'::jsonb)->>'dup' = '5' and (select count(*) from public.batches) = :nb0, '全部已在列表：不发、不建空批次');
select pg_temp.ok((select sent_count from public.sel_batches where batch_no = 3) = 5, '全重复批次也标记已发送');
select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000002']::uuid[], true) as r \gset
select pg_temp.ok((:'r'::jsonb)->>'sent' = '0' and (select count(*) from public.batches) = :nb0, '再发一次（p_resend）：全部已在列表 → 不重复');
delete from public.jobs where etsy_listing_id in (20, 21);   -- 预筛选里彻底删除（025）后可以补发
select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001'::uuid], true) as r \gset
select pg_temp.ok((:'r'::jsonb)->>'sent' = '2', '重发只补发列表里没有的 2 条');
select pg_temp.ok((select count(*) - count(distinct etsy_listing_id) from public.jobs where user_id = :'u1' and prescreen and screen_hidden_at is null) = 0, '最终：u1 列表里没有重复');
\echo ALL 041 TESTS PASSED
