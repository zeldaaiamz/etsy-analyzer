-- 048 本地测试：进行中也能查询新词
\set u1 '11111111-1111-1111-1111-111111111111'
insert into auth.users values (:'u1');
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;
select set_config('request.jwt.claim.sub', :'u1', false);
select public.sel_count('notebooks') as a \gset
update public.sel_runs set status = 'running', queue_at = now() where id = :'a';
select public.sel_count('sheet') as b \gset
select pg_temp.ok(:'b' <> :'a', '抓取进行中可查询新词 sheet');
select public.sel_count('mug') as c \gset
select pg_temp.ok(:'c' <> :'b', 'sheet 还在查询数量时，mug 也能排队');
select pg_temp.ok(public.sel_count('  SHEET ') = :'b', '同一词连点 → 返回同一个任务（幂等）');
select pg_temp.ok((select count(*) from public.sel_runs) = 3, '共 3 个任务，无重复');
select pg_temp.ok((select count(*) from public.sel_keyword_log where keyword_norm = 'sheet') = 1, '043 记录只记 1 次');
reset role;
select pg_temp.ok((public.sel_claim_run('w')).id = :'b', 'worker 认领顺序：先 sheet（先排队的查询数量）');
select pg_temp.ok((public.sel_claim_run('w')).id = :'c', '再 mug');
select 'ALL 048 TESTS PASSED';
