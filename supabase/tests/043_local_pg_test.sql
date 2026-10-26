-- 043 本地测试：关键词查询记录永久保留
\set u1 '11111111-1111-1111-1111-111111111111'
\set u2 '22222222-2222-2222-2222-222222222222'
insert into auth.users values (:'u1'), (:'u2');
create or replace function pg_temp.as_user(u uuid) returns void language sql as $$ select set_config('request.jwt.claim.sub', u::text, false) $$;
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;
create or replace function pg_temp.blocked(q text) returns boolean language plpgsql as $$ begin execute q; return false; exception when others then return sqlerrm like '%永久%'; end $$;
-- 预筛选旧批次（回填来源 B）
insert into public.batches (user_id, name, created_at) values (:'u1', '获取链接 Wedding  Sign 第1批 10-10 09:00', '2026-10-10 01:00Z'), (:'u1', '获取链接 wedding sign 第2批 10-10 10:00', '2026-10-10 02:00Z'), (:'u1', '手动批次', now());
select pg_temp.as_user(:'u1');
select public.sel_count('acrylic decoration') as r1 \gset
update public.sel_runs set status = 'succeeded', finished_at = now() - interval '10 days', created_at = now() - interval '10 days' where id = :'r1';
select pg_temp.ok((select count(*) from public.sel_keyword_log where run_id = :'r1') = 1, 'sel_count 提交 → 记录 1 条');
select pg_temp.ok(cardinality(public.sel_keyword_seen('  ACRYLIC   Decoration ')) = 1, '大小写/空格不敏感命中');
select pg_temp.ok(cardinality(public.sel_keyword_seen('gift')) = 0, '未查询过的词返回空');
select public.sel_count('Acrylic Decoration') as r2 \gset
select pg_temp.ok(cardinality(public.sel_keyword_seen('acrylic decoration')) = 2, '再次查询 → 2 个日期');
select pg_temp.ok((public.sel_keyword_seen('acrylic decoration'))[1] > (public.sel_keyword_seen('acrylic decoration'))[2], '新的在前');
select pg_temp.as_user(:'u2');
select pg_temp.ok(cardinality(public.sel_keyword_seen('acrylic decoration')) = 0, '别人的记录不可见');
select pg_temp.as_user(:'u1');
-- 删除路径
update public.sel_runs set status = 'succeeded' where id = :'r2';
select pg_temp.ok(public.sel_delete_run(:'r2'), '逐条删除历史 sel_delete_run 成功');
select pg_temp.ok((select count(*) from public.sel_runs) = 1 and (select count(*) from public.sel_keyword_log) = 2, '删除历史后记录仍 2 条');
select public.sel_purge_older_than() as p \gset
select pg_temp.ok((select count(*) from public.sel_runs) = 0, '3 天清理删掉了旧 run');
select pg_temp.ok((select count(*) from public.sel_keyword_log) = 2, '3 天清理后记录仍 2 条');
select pg_temp.ok(pg_temp.blocked('delete from public.sel_keyword_log'), 'DELETE 被阻止（postgres 超级用户）');
select pg_temp.ok(pg_temp.blocked('update public.sel_keyword_log set keyword = ''x'''), 'UPDATE 被阻止');
select pg_temp.ok(pg_temp.blocked('truncate public.sel_keyword_log'), 'TRUNCATE 被阻止');
select pg_temp.ok(pg_temp.blocked('truncate public.sel_runs, public.sel_keyword_log cascade'), 'TRUNCATE ... CASCADE 被阻止');
select pg_temp.ok(pg_temp.blocked('set session_replication_role = replica; delete from public.sel_keyword_log'), 'replica 模式下 DELETE 也被阻止');
reset session_replication_role;
select pg_temp.ok(pg_temp.blocked('drop table public.sel_keyword_log'), 'DROP TABLE 被事件触发器阻止');
select pg_temp.ok(pg_temp.blocked('drop table public.sel_keyword_log cascade'), 'DROP ... CASCADE 被阻止');
delete from auth.users where id = :'u2';
select pg_temp.ok(not exists (select 1 from pg_constraint where confrelid = 'public.sel_keyword_log'::regclass or conrelid = 'public.sel_keyword_log'::regclass and contype = 'f'), '无外键（不会被级联删除）');
select pg_temp.ok(not has_table_privilege('authenticated', 'public.sel_keyword_log', 'DELETE') and not has_table_privilege('service_role', 'public.sel_keyword_log', 'TRUNCATE') and not has_table_privilege('authenticated', 'public.sel_keyword_log', 'INSERT'), '权限已收回');
-- 作为 authenticated 角色直接删 → 权限错误
set role authenticated;
select pg_temp.ok((select count(*) from public.sel_keyword_log) = 2, 'authenticated 只读到自己的');
do $$ begin delete from public.sel_keyword_log; raise exception 'FAIL: authenticated 能删'; exception when insufficient_privilege then raise notice 'ok: authenticated DELETE 无权限'; end $$;
reset role;
-- 迁移重跑不丢数据
\i migrations/043_keyword_log.sql
select pg_temp.ok((select count(*) from public.sel_keyword_log) = 2, '重跑 043 后数据仍在');
-- 回填（可重复）
\i backfill/043_backfill_keyword_log.sql
\i backfill/043_backfill_keyword_log.sql
select pg_temp.ok((select count(*) from public.sel_keyword_log where source = 'backfill_batches') = 1, '回填：同日两批 wedding sign 合并 1 条，手动批次忽略，重复执行不重复');
select pg_temp.ok(cardinality(public.sel_keyword_seen('wedding sign')) = 1, '回填的记录能被提示命中');
