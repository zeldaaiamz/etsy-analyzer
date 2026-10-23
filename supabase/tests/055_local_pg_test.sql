-- 055：delete_library_items 直接删除；不回任务列表；不碰 scrape/reviews；权限
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$
begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;

create or replace function public.remove_from_library(p_items uuid[] default null, p_jobs uuid[] default null) returns int
language plpgsql security definer set search_path = public as $$
declare n int;
begin
  delete from public.product_library l
   where l.user_id = (select auth.uid())
     and (l.id = any(coalesce(p_items, '{}')) or l.job_id = any(coalesce(p_jobs, '{}')));
  get diagnostics n = row_count; return n;
end $$;

\i migrations/055_library_remove_delete.sql

-- 种子：用户 1111 有库条目 + 任务；用户 2222 有另一条
insert into public.batches (id, user_id, name) values
  ('b1111111-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', 'b1'),
  ('b2222222-2222-2222-2222-222222222222', '22222222-2222-2222-2222-222222222222', 'b2');

insert into public.jobs (id, batch_id, user_id, etsy_listing_id, status) values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'b1111111-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', 5001, 'succeeded'),
  ('aaaaaaaa-0000-0000-0000-000000000002', 'b1111111-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', 5001, 'succeeded'), -- 同 listing 额外任务
  ('aaaaaaaa-0000-0000-0000-000000000003', 'b1111111-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', 5002, 'succeeded'),
  ('aaaaaaaa-0000-0000-0000-000000000004', 'b1111111-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', 5003, 'succeeded'),
  ('aaaaaaaa-0000-0000-0000-000000000099', 'b1111111-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', 5099, 'running'),
  ('bbbbbbbb-0000-0000-0000-000000000001', 'b2222222-2222-2222-2222-222222222222', '22222222-2222-2222-2222-222222222222', 6001, 'succeeded');

insert into public.etsy_products (job_id, title, listing_id) values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'A1', 5001),
  ('aaaaaaaa-0000-0000-0000-000000000003', 'A3', 5002);

insert into public.analyses (job_id, product_summary) values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'sum1'),
  ('aaaaaaaa-0000-0000-0000-000000000002', 'sum2');

insert into public.evidence_snapshots (job_id, image_path) values
  ('aaaaaaaa-0000-0000-0000-000000000001', '1111/aaaaaaaa-0000-0000-0000-000000000001/01.png');

insert into public.product_library (id, user_id, job_id, etsy_listing_id, stage) values
  ('cccccccc-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-0000-0000-0000-000000000001', 5001, 'candidate'),
  ('cccccccc-0000-0000-0000-000000000002', '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-0000-0000-0000-000000000003', 5002, 'rejected'),
  ('cccccccc-0000-0000-0000-000000000003', '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-0000-0000-0000-000000000004', 5003, 'rejected'),
  ('cccccccc-0000-0000-0000-000000000099', '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-0000-0000-0000-000000000099', 5099, 'candidate'),
  ('dddddddd-0000-0000-0000-000000000001', '22222222-2222-2222-2222-222222222222', 'bbbbbbbb-0000-0000-0000-000000000001', 6001, 'candidate');

insert into public.listing_scrape_status (listing_id, status) values (5001, 'ok'), (5002, 'failed'), (6001, 'ok');
insert into public.listing_reviews (listing_id, review_date) values (5001, '2026-10-01'), (5002, '2026-10-02');

-- 对照：旧 remove_from_library 会让任务回到 task_jobs
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', false);
select pg_temp.ok((select count(*) from public.task_jobs where user_id = '11111111-1111-1111-1111-111111111111' and etsy_listing_id = 5001) = 0,
  '删除前：库内 listing 不在我的任务');

-- 1) delete_library_items：删库 + 同 listing 全部非 running 任务；scrape/reviews 保留
select set_config('pg_temp.last', public.delete_library_items(array['cccccccc-0000-0000-0000-000000000001'::uuid], null)::text, false);
select pg_temp.ok((current_setting('pg_temp.last')::jsonb->>'deleted_library')::int = 1, '删 1 条产品库');
select pg_temp.ok((current_setting('pg_temp.last')::jsonb->>'deleted_jobs')::int = 2, '同 listing 两个 succeeded 任务都删');
select pg_temp.ok(not exists (select 1 from public.product_library where id = 'cccccccc-0000-0000-0000-000000000001'), '库行已删');
select pg_temp.ok(not exists (select 1 from public.jobs where id in ('aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-0000-0000-0000-000000000002')), '任务已删');
select pg_temp.ok(not exists (select 1 from public.analyses where job_id = 'aaaaaaaa-0000-0000-0000-000000000001'), '分析 cascade 已删');
select pg_temp.ok((select count(*) from public.task_jobs where etsy_listing_id = 5001) = 0, '不回我的任务（任务已物理删除）');
select pg_temp.ok((select status from public.listing_scrape_status where listing_id = 5001) = 'ok', 'listing_scrape_status 保留');
select pg_temp.ok((select count(*) from public.listing_reviews where listing_id = 5001) = 1, 'listing_reviews 保留');
select pg_temp.ok((current_setting('pg_temp.last')::jsonb->'paths') ?| array['1111/aaaaaaaa-0000-0000-0000-000000000001/01.png']
  or (current_setting('pg_temp.last')::jsonb->>'paths') like '%01.png%', '返回截图 paths');

-- 2) running 跳过：库行可删，running 任务保留
select set_config('pg_temp.last', public.delete_library_items(array['cccccccc-0000-0000-0000-000000000099'::uuid], null)::text, false);
select pg_temp.ok((current_setting('pg_temp.last')::jsonb->>'deleted_library')::int = 1, 'running 关联：库行仍删');
select pg_temp.ok((current_setting('pg_temp.last')::jsonb->>'skipped_running')::int = 1, 'running 计入 skipped');
select pg_temp.ok(exists (select 1 from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000099' and status = 'running'), 'running 任务保留');
select pg_temp.ok(exists (select 1 from public.task_jobs where id = 'aaaaaaaa-0000-0000-0000-000000000099'), 'running 任务因已出库而出现在我的任务（预期边缘）');

-- 3) delete_rejected_library：批量删 rejected + 任务
select set_config('pg_temp.last', public.delete_rejected_library()::text, false);
select pg_temp.ok((current_setting('pg_temp.last')::jsonb->>'deleted_library')::int = 2, '不考虑两条都删');
select pg_temp.ok(not exists (select 1 from public.product_library where user_id = '11111111-1111-1111-1111-111111111111' and stage = 'rejected'), '无剩余 rejected');
select pg_temp.ok(not exists (select 1 from public.jobs where id in ('aaaaaaaa-0000-0000-0000-000000000003','aaaaaaaa-0000-0000-0000-000000000004')), 'rejected 对应任务已删');
select pg_temp.ok((select status from public.listing_scrape_status where listing_id = 5002) = 'failed', 'rejected 的 scrape 仍保留');

-- 4) 不能删别人的
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', false);
select set_config('pg_temp.last', public.delete_library_items(array['dddddddd-0000-0000-0000-000000000001'::uuid], null)::text, false);
select pg_temp.ok((current_setting('pg_temp.last')::jsonb->>'deleted_library')::int = 0, '不能删别人库');
select pg_temp.ok(exists (select 1 from public.product_library where id = 'dddddddd-0000-0000-0000-000000000001'), '别人库行仍在');
select pg_temp.ok(exists (select 1 from public.jobs where id = 'bbbbbbbb-0000-0000-0000-000000000001'), '别人任务仍在');

-- 5) remove_from_library 仍只移出库（撤销加入场景）
insert into public.jobs (id, batch_id, user_id, etsy_listing_id, status) values
  ('aaaaaaaa-0000-0000-0000-000000000010', 'b1111111-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', 5010, 'succeeded');
insert into public.product_library (id, user_id, job_id, etsy_listing_id, stage) values
  ('cccccccc-0000-0000-0000-000000000010', '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-0000-0000-0000-000000000010', 5010, 'candidate');
select pg_temp.ok((select count(*) from public.task_jobs where id = 'aaaaaaaa-0000-0000-0000-000000000010') = 0, '撤销场景：入库后不在任务列表');
select public.remove_from_library(array['cccccccc-0000-0000-0000-000000000010'::uuid], null);
select pg_temp.ok(not exists (select 1 from public.product_library where id = 'cccccccc-0000-0000-0000-000000000010'), 'remove_from_library 只删库');
select pg_temp.ok(exists (select 1 from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000010'), 'remove_from_library 保留任务');
select pg_temp.ok(exists (select 1 from public.task_jobs where id = 'aaaaaaaa-0000-0000-0000-000000000010'), 'remove_from_library 后回到我的任务');

-- 6) 权限
select pg_temp.ok(
  has_function_privilege('authenticated', 'public.delete_library_items(uuid[], uuid[])', 'execute')
  and not has_function_privilege('anon', 'public.delete_library_items(uuid[], uuid[])', 'execute')
  and has_function_privilege('authenticated', 'public.delete_rejected_library()', 'execute')
  and not has_function_privilege('anon', 'public.delete_rejected_library()', 'execute'),
  '权限：仅 authenticated');

-- 7) 不碰 sel_keyword_log
create table if not exists public.sel_keyword_log (id bigserial primary key, keyword text not null, created_at timestamptz not null default now());
insert into public.sel_keyword_log (keyword) values ('gift-055');
select set_config('pg_temp.kw_before', (select count(*)::text from public.sel_keyword_log), false);
select set_config('request.jwt.claim.sub', '22222222-2222-2222-2222-222222222222', false);
select public.delete_library_items(array['dddddddd-0000-0000-0000-000000000001'::uuid], null);
select pg_temp.ok((select count(*) from public.sel_keyword_log) = current_setting('pg_temp.kw_before')::int, '不碰 sel_keyword_log');

-- 8) 幂等迁移
\i migrations/055_library_remove_delete.sql
select pg_temp.ok(true, '055 迁移幂等');

select 'ALL 055 TESTS PASSED';
