-- 039 本地测试：psql -v ON_ERROR_STOP=1 -f 039_local_pg_test.sql（在 stub + 024…039 之后）
\set u1 '11111111-1111-1111-1111-111111111111'
\set u2 '22222222-2222-2222-2222-222222222222'
insert into auth.users values (:'u1'), (:'u2');
-- 造数据：run A：第 1 批 1200 条通过（>500 → 拆 3 个预筛选批次），第 2 批 30 条（其中 5 条与第 1 批重复）
insert into public.sel_runs (id, user_id, keyword, status) values ('aaaaaaaa-0000-0000-0000-000000000001', :'u1', 'acrylic sign', 'succeeded');
insert into public.sel_batches (id, run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end, status) values
 ('bbbbbbbb-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001', :'u1', 1, 1, 1000, 1, 50, 'done'),
 ('bbbbbbbb-0000-0000-0000-000000000002', 'aaaaaaaa-0000-0000-0000-000000000001', :'u1', 2, 1001, 2000, 51, 100, 'done'),
 ('bbbbbbbb-0000-0000-0000-000000000003', 'aaaaaaaa-0000-0000-0000-000000000001', :'u1', 3, 2001, 3000, 101, 150, 'running');
insert into public.sel_items (run_id, batch_id, user_id, batch_no, etsy_listing_id, rank, verdict)
 select 'aaaaaaaa-0000-0000-0000-000000000001', 'bbbbbbbb-0000-0000-0000-000000000001', :'u1', 1, 1000000 + g, g, '保留' from generate_series(1, 700) g;
insert into public.sel_items (run_id, batch_id, user_id, batch_no, etsy_listing_id, rank, verdict)
 select 'aaaaaaaa-0000-0000-0000-000000000001', 'bbbbbbbb-0000-0000-0000-000000000002', :'u1', 2, 2000000 + g, 1000 + g, '待确认' from generate_series(1, 30) g;
create or replace function pg_temp.as_user(u uuid) returns void language sql as $$ select set_config('request.jwt.claim.sub', u::text, false) $$;
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;

-- ① 未登录
do $$ begin perform public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001'::uuid]); raise exception 'FAIL: 未登录应报错'; exception when others then if sqlerrm like 'FAIL%' then raise; end if; raise notice 'ok: 未登录拒绝（%）', sqlerrm; end $$;
-- ② 别人的批次：什么都不做
select pg_temp.as_user(:'u2');
select pg_temp.ok((public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001'::uuid]))->>'sent' = '0', '不能发别人的批次');
select pg_temp.ok((select count(*) from public.batches) = 0, '别人调用不建预筛选批次');
-- ③ 第 1 批 700 条 → 2 个预筛选批次（500 + 200），全部发完、标记 sent_count=700
select pg_temp.as_user(:'u1');
select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001'::uuid]) as r \gset
select pg_temp.ok((:'r'::jsonb)->>'sent' = '700' and (:'r'::jsonb)->>'screen_batches' = '2', '700 条拆成 2 个预筛选批次');
select pg_temp.ok((select max(c) from (select count(*) c from public.jobs group by batch_id) x) = 500, '每个预筛选批次 ≤500');
select pg_temp.ok((select sent_count = 700 and sent_to_screen_at is not null from public.sel_batches where batch_no = 1 and run_id = 'aaaaaaaa-0000-0000-0000-000000000001'), '第 1 批 sent_count = 700');
select pg_temp.ok((select bool_and(prescreen and status = 'queued' and user_id = :'u1') from public.jobs), 'jobs 是本人的预筛选排队任务');
-- ④ 连点 / 再调一次（不带 p_resend）：跳过，不建新批次
select pg_temp.ok((public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001'::uuid]))->'batches'->0->>'skipped' = 'already', '已发送的批次再调一次 → 跳过');
select pg_temp.ok((select count(*) from public.batches) = 2 and (select count(*) from public.jobs) = 700, '重复调用没有多建批次/任务');
-- ⑤ 上限 1000：第 2 批 30 条 + 再发第 1 批（重发 700）→ 额度只剩 300，全发不下：按顺序发 300，标记部分
update public.jobs set status = 'succeeded' where etsy_listing_id <= 1000300;   -- 300 条已跑完 → 排队+运行中 400，额度 600
select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001'::uuid], true) as r \gset
select pg_temp.ok((:'r'::jsonb)->>'sent' = '600' and (:'r'::jsonb)->>'limited' = 'true', '额度 600：重发 700 条只发了 600，limited');
select pg_temp.ok((select sent_count from public.sel_batches where id = 'bbbbbbbb-0000-0000-0000-000000000001') = 600, '部分：sent_count = 600/700');
-- ⑥ 额度为 0：报错，事务整体回滚（没有空批次）
select count(*) as nb0 from public.batches \gset
do $$ begin perform public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000002'::uuid]); raise exception 'FAIL: 额度 0 应报错'; exception when others then if sqlerrm like 'FAIL%' then raise; end if; raise notice 'ok: 额度 0 报错：%', sqlerrm; end $$;
select pg_temp.ok((select count(*) from public.batches) = :nb0, '额度 0：没有留下空批次');
select pg_temp.ok((select sent_count is null and sent_to_screen_at is null from public.sel_batches where id = 'bbbbbbbb-0000-0000-0000-000000000002'), '额度 0：第 2 批仍是未发送');
-- ⑦ 「发送剩余」：额度恢复后，第 1 批只发剩下的 100 条；发送全部批次：第 2 批 30 条一起，正在读的第 3 批忽略
update public.jobs set status = 'succeeded';
select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000002','bbbbbbbb-0000-0000-0000-000000000003']::uuid[]) as r \gset
select pg_temp.ok((:'r'::jsonb)->>'sent' = '130' and jsonb_array_length((:'r'::jsonb)->'batches') = 2, '发送全部：第 1 批剩余 100 + 第 2 批 30，进行中的第 3 批不发');
select pg_temp.ok((select min(etsy_listing_id) from public.jobs j join public.batches b on b.id = j.batch_id where b.created_at = (select max(created_at) from public.batches) and etsy_listing_id < 2000000) = 1000601, '剩余从第 601 条接着发');
select pg_temp.ok((select bool_and(sent_count = (select count(*) from public.sel_items i where i.batch_id = sb.id)) from public.sel_batches sb where status = 'done'), '两批都标记为全部发送');
-- ⑧ 038 时代的标记（sent_count 为空）视为整批已发送
update public.sel_batches set sent_count = null where batch_no = 2;
select pg_temp.ok((public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000002'::uuid]))->'batches'->0->>'skipped' = 'already', '038 旧标记：视为已发送');
-- ⑨ 发送全部（跨任务）时重复的 listing 只建一条 job：run B 的批次含 1000001（与 run A 第 1 批重复）+ 3000001
insert into public.sel_runs (id, user_id, keyword, status, created_at) values ('aaaaaaaa-0000-0000-0000-000000000002', :'u1', 'sign', 'succeeded', now() + interval '1 second');
insert into public.sel_batches (id, run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end, status) values ('bbbbbbbb-0000-0000-0000-000000000009', 'aaaaaaaa-0000-0000-0000-000000000002', :'u1', 1, 1, 20, 1, 1, 'failed');
insert into public.sel_items (run_id, batch_id, user_id, batch_no, etsy_listing_id, rank, verdict) values ('aaaaaaaa-0000-0000-0000-000000000002', 'bbbbbbbb-0000-0000-0000-000000000009', :'u1', 1, 1000001, 1, '保留'), ('aaaaaaaa-0000-0000-0000-000000000002', 'bbbbbbbb-0000-0000-0000-000000000009', :'u1', 1, 3000001, 2, '保留');
select count(*) as j0 from public.jobs \gset
select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000001','bbbbbbbb-0000-0000-0000-000000000009']::uuid[], true) as r \gset
select pg_temp.ok((select count(*) from public.jobs) - :j0 = 701, '702 条链接里重复 1 条 → 只建 701 个 job');
select pg_temp.ok((select sent_count from public.sel_batches where id = 'bbbbbbbb-0000-0000-0000-000000000009') = 2, '重复的那条也算本批已发送（2/2），失败的批次也能发');
-- ⑩ 权限
select pg_temp.ok(has_function_privilege('authenticated', 'public.sel_send_to_screen(uuid[], boolean)', 'execute') and not has_function_privilege('anon', 'public.sel_send_to_screen(uuid[], boolean)', 'execute'), 'authenticated 可调用，anon 不可');
\echo ALL 039 TESTS PASSED
