-- 051 本地测试：确认 → 我的任务最前；加产品库 → 算 L；删除/撤销；task_list total；他人数据不可见
\set u1 '11111111-1111-1111-1111-111111111111'
\set u2 '22222222-2222-2222-2222-222222222222'
insert into auth.users values (:'u1'), (:'u2');
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;
select set_config('request.jwt.claim.sub', :'u1', false);
insert into batches (id, name, created_at) values ('aaaaaaaa-0000-0000-0000-000000000001', '获取链接 book 第1批', now() - interval '1 day');
-- 35 条普通任务（较新发送）→ 原排序下把 book 挤出首屏 30 条
insert into jobs (etsy_listing_id, status, created_at) select 9000 + g, 'succeeded', now() - interval '1 hour' + g * interval '1 second' from generate_series(1, 35) g;
-- book：6 条已取到待确认（昨天发送）+ 2 条将被删除
insert into jobs (batch_id, etsy_listing_id, status, prescreen, screen_state, created_at)
  select 'aaaaaaaa-0000-0000-0000-000000000001', 100 + g, 'screening', true, 'awaiting', now() - interval '1 day' from generate_series(1, 8) g;
insert into etsy_products (job_id, title, image_url) select id, 'Book ' || etsy_listing_id, 'https://i.etsystatic.com/x/il_570xN.' || etsy_listing_id || '.jpg' from jobs where prescreen;
-- 他人数据
insert into jobs (user_id, batch_id, etsy_listing_id, status, prescreen, screen_state, screened_at) values (:'u2', 'aaaaaaaa-0000-0000-0000-000000000001', 777, 'queued', true, 'kept', now());
-- 取数失败、从未取到（screen_state 为空）的条目：不算确认、不算缺（生产 postcard 批次曾被误算为缺 1）
insert into jobs (batch_id, etsy_listing_id, status, prescreen, screen_state, error) values ('aaaaaaaa-0000-0000-0000-000000000001', 199, 'failed', true, null, '网页取数失败');
select public.screen_confirm(array(select id from jobs where prescreen and user_id = :'u1' and etsy_listing_id between 101 and 106));
update jobs set screen_hidden_at = now() where etsy_listing_id in (107, 108);
select pg_temp.ok((public.task_list(30)->>'total')::int = 41, 'total = 35 普通 + 6 确认');
select pg_temp.ok((select count(*) from jsonb_array_elements(public.task_list(30)->'jobs') r where (r->>'etsy_listing_id')::bigint between 101 and 106) = 6, '刚确认的 6 条在首屏 30 条内');
select pg_temp.ok((public.task_list(30)->'jobs'->0->>'etsy_listing_id')::bigint between 101 and 106, '刚确认的排最前');
select pg_temp.ok((public.task_list(30)->>'has_more')::boolean, '还有更多（41 > 30）');
select public.screen_reconcile() -> 0 as r \gset
select pg_temp.ok((:'r'::jsonb->>'confirmed')::int = 6 and (:'r'::jsonb->>'in_tasks')::int = 6 and (:'r'::jsonb->>'missing')::int = 0, '对账：确认 6 · 已进任务 6 · 缺 0（绿；取数失败未确认的不算缺）');
select pg_temp.ok((:'r'::jsonb->>'deleted')::int = 2 and jsonb_array_length(:'r'::jsonb->'deleted_items') = 2, '已删除 2 条单列');
select pg_temp.ok((select bool_and((i->>'task_row')::int <= 6) from jsonb_array_elements(:'r'::jsonb->'items') i), '行号 1–6');
select pg_temp.ok(jsonb_array_length(public.screen_reconcile()) = 1, '他人数据不可见（u2 的确认不计入）');
-- 加入产品库 → 计入 L，不算缺
insert into product_library (user_id, job_id, etsy_listing_id) select user_id, id, etsy_listing_id from jobs where etsy_listing_id = 101 and user_id = :'u1';
select public.screen_reconcile() -> 0 as r \gset
select pg_temp.ok((:'r'::jsonb->>'in_tasks')::int = 5 and (:'r'::jsonb->>'in_library')::int = 1 and (:'r'::jsonb->>'missing')::int = 0, '产品库：M 5 · L 1 · 缺 0');
-- 分析失败仍在任务里：计为已进任务 + failed
update jobs set status = 'failed', error = '未能识别出亚马逊搜索词' where etsy_listing_id = 102;
select public.screen_reconcile() -> 0 as r \gset
select pg_temp.ok((:'r'::jsonb->>'failed')::int = 1 and (:'r'::jsonb->>'in_tasks')::int = 5, '失败计入已进任务并单独标出');
-- 模拟真正缺失（确认了但既不在任务也不在产品库：例如视图被改坏）→ missing
create or replace view public.task_jobs as select id, batch_id, user_id, etsy_url, etsy_listing_id, status, stage, attempts, max_attempts, error, locked_by, locked_at, run_after, created_at, updated_at,
  finished_at, queued_at, started_at, reviewed_at, cancel_requested, prescreen, screen_state, screened_at from jobs j where etsy_listing_id <> 103 and ((not prescreen) or (screen_state = 'kept'))
  and ((etsy_listing_id is null) or (not exists (select 1 from product_library l where l.user_id = j.user_id and l.etsy_listing_id = j.etsy_listing_id)));
select public.screen_reconcile() -> 0 as r \gset
select pg_temp.ok((:'r'::jsonb->>'missing')::int = 1 and exists (select 1 from jsonb_array_elements(:'r'::jsonb->'items') i where i->>'reason' = 'missing' and (i->>'listing_id')::bigint = 103), '真缺失 → 缺 1（红）并列出 103');
-- 撤销删除
select pg_temp.ok(public.screen_unhide(array(select id from jobs where etsy_listing_id = 107)) = 1, '撤销删除 1 条');
select pg_temp.ok(((public.screen_reconcile() -> 0)->>'deleted')::int = 1, '删除数 2 → 1');
-- 性能：新排序可走索引
set enable_seqscan = off;
do $$ declare l text; plan text := ''; begin
  for l in execute 'explain select id from public.jobs where user_id = ''11111111-1111-1111-1111-111111111111'' order by coalesce(screened_at, created_at) desc limit 31' loop plan := plan || l; end loop;
  perform pg_temp.ok(position('jobs_user_sort_idx' in plan) > 0, '排序索引可用：' || left(plan, 80)); end $$;
select 'ALL 051 TESTS PASSED';
