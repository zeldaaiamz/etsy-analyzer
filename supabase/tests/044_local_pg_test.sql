-- 044 本地测试：获取链接 → 预筛选 自动发送（worker 以 service 身份更新 sel_batches.status，不带登录）
\set u1 '11111111-1111-1111-1111-111111111111'
\set u2 '22222222-2222-2222-2222-222222222222'
insert into auth.users values (:'u1'), (:'u2');
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;
create or replace function pg_temp.mk(run uuid, bid uuid, u uuid, kw text, base int, n int, st text) returns void language sql as $$
  insert into public.sel_runs (id, user_id, keyword, status) values (run, u, kw, 'running') on conflict do nothing;
  insert into public.sel_batches (id, run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end, status) values (bid, run, u, 1, 1, n, 1, 1, st);
  insert into public.sel_items (run_id, batch_id, user_id, batch_no, etsy_listing_id, rank, verdict) select run, bid, u, 1, base + g, g, '保留' from generate_series(1, n) g; $$;
select set_config('request.jwt.claim.sub', '', false);
-- ① 历史批次（044 之前就已读完）不会被自动发送
select pg_temp.mk('a0000000-0000-0000-0000-000000000000', 'b0000000-0000-0000-0000-000000000000', :'u1', 'old', 100, 5, 'done');
select pg_temp.ok((select count(*) from public.jobs) = 0, '历史已读完批次不自动发送');
-- ② 读完 → 自动发送全部 30 条，auto_sent_at 有值，sent_count = 30
select pg_temp.mk('a0000000-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000001', :'u1', 'gift', 1000, 30, 'running');
update public.sel_batches set status = 'done' where id = 'b0000000-0000-0000-0000-000000000001';
select pg_temp.ok((select count(*) from public.jobs where etsy_listing_id between 1001 and 1030) = 30, 'worker 把批次改成 done → 自动发送 30 条');
select pg_temp.ok((select sent_count = 30 and auto_sent_at is not null and not auto_pending and auto_error is null from public.sel_batches where id = 'b0000000-0000-0000-0000-000000000001'), 'sent_count=30，已自动发送，无待补发');
select pg_temp.ok((select name like '获取链接 gift 第1批%' from public.batches limit 1), '预筛选批次命名不变');
-- ③ 幂等：再次触发（状态来回改 / 手动再点发送）不重复
update public.sel_batches set status = 'running' where id = 'b0000000-0000-0000-0000-000000000001';
update public.sel_batches set status = 'done' where id = 'b0000000-0000-0000-0000-000000000001';
select pg_temp.ok((select count(*) from public.jobs) = 30, '再次变成 done 不重复发送');
select set_config('request.jwt.claim.sub', :'u1', false);
select pg_temp.ok((public.sel_send_to_screen(array['b0000000-0000-0000-0000-000000000001'::uuid]) ->'batches'->0->>'skipped') = 'already', '手动发送已自动发完的批次：跳过');
select set_config('request.jwt.claim.sub', '', false);
-- ④ 部分发送 + 自动补发：预筛选已有 990 条排队 → 30 条只发 10 条；腾出额度后自动发剩余，每条只发一次
update public.jobs set status = 'succeeded';
insert into public.batches (id, user_id, name) select ('e0000000-0000-0000-0000-00000000000' || g)::uuid, :'u1', 'fill' from generate_series(0, 1) g;
insert into public.jobs (batch_id, user_id, etsy_url, etsy_listing_id, prescreen) select ('e0000000-0000-0000-0000-00000000000' || (g % 2))::uuid, :'u1', 'https://www.etsy.com/listing/' || (900000 + g), 900000 + g, true from generate_series(1, 990) g;
select pg_temp.mk('a0000000-0000-0000-0000-000000000002', 'b0000000-0000-0000-0000-000000000002', :'u1', 'reason', 2000, 30, 'running');
update public.sel_batches set status = 'done' where id = 'b0000000-0000-0000-0000-000000000002';
select pg_temp.ok((select sent_count = 10 and auto_pending from public.sel_batches where id = 'b0000000-0000-0000-0000-000000000002'), '额度满：只发前 10 条，标记待补发');
update public.jobs set status = 'succeeded' where etsy_listing_id between 900001 and 900015;
select pg_temp.ok((select sent_count = 25 and auto_pending from public.sel_batches where id = 'b0000000-0000-0000-0000-000000000002'), '腾出 15 条 → 自动补发 15 条（sent_count 25）');
select pg_temp.ok((select array_agg(etsy_listing_id order by etsy_listing_id)::int[] = array(select 2000 + g from generate_series(1, 25) g) from public.jobs where etsy_listing_id between 2001 and 2030), '已发的正好是 rank 前 25 条（每条链接「已发送」勾选 = 前 sent_count 条，准确）');
update public.jobs set status = 'failed' where etsy_listing_id between 900016 and 900030;
select pg_temp.ok((select sent_count = 30 and not auto_pending from public.sel_batches where id = 'b0000000-0000-0000-0000-000000000002'), '再腾出 → 发完剩余 5 条，不再待补发');
select pg_temp.ok((select count(*) = 30 and count(distinct etsy_listing_id) = 30 from public.jobs where etsy_listing_id between 2001 and 2030), '30 条每条恰好 1 个任务');
-- ⑤ 关闭开关：读完不自动发送；手动仍可发
insert into public.sel_prefs (user_id, auto_send) values (:'u2', false);
select pg_temp.mk('a0000000-0000-0000-0000-000000000003', 'b0000000-0000-0000-0000-000000000003', :'u2', 'off', 3000, 5, 'running');
update public.sel_batches set status = 'done' where id = 'b0000000-0000-0000-0000-000000000003';
select pg_temp.ok((select count(*) from public.jobs where user_id = :'u2') = 0 and (select sent_to_screen_at is null from public.sel_batches where id = 'b0000000-0000-0000-0000-000000000003'), '关闭自动发送：不发送');
select set_config('request.jwt.claim.sub', :'u2', false);
select pg_temp.ok((public.sel_send_to_screen(array['b0000000-0000-0000-0000-000000000003'::uuid]) ->>'sent') = '5', '关闭时手动发送照常');
select pg_temp.ok((select auto_sent_at is null from public.sel_batches where id = 'b0000000-0000-0000-0000-000000000003'), '手动发送不标「已自动发送」');
-- ⑥ 停止 / 失败的批次不自动发送
select set_config('request.jwt.claim.sub', '', false);
select pg_temp.mk('a0000000-0000-0000-0000-000000000004', 'b0000000-0000-0000-0000-000000000004', :'u1', 'stop', 4000, 5, 'running');
update public.sel_batches set status = 'canceled' where id = 'b0000000-0000-0000-0000-000000000004';
select pg_temp.ok((select count(*) from public.jobs where etsy_listing_id between 4001 and 4005) = 0, '停止的批次不自动发送');
-- ⑦ RLS：只能读写自己的开关
select pg_temp.ok((select count(*) from public.sel_prefs) = 1, 'sel_prefs 一行');
