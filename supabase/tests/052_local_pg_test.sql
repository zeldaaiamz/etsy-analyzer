-- 052：查数量成功后自动入队；0 条不抓；已有任务时仍排队；取消可停
\set u1 '11111111-1111-1111-1111-111111111111'
insert into auth.users values (:'u1') on conflict do nothing;
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;
select set_config('request.jwt.claim.sub', :'u1', false);

-- 1) 先建一个 running，再 count → counted 自动入队（不拦并发）
select public.sel_count('sheet') as a \gset
update public.sel_runs set status = 'running', queue_at = now(), total_count = 100, max_rows = 100 where id = :'a';
insert into public.sel_batches (run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end, status)
values (:'a', :'u1'::uuid, 1, 1, 100, 1, 5, 'running');

select public.sel_count('mug') as b \gset
-- worker 写 counted（触发器自动入队）
update public.sel_runs set status = 'counting' where id = :'b';
update public.sel_runs set status = 'counted', total_count = 2500, counted_at = now() where id = :'b';
select pg_temp.ok((select status from public.sel_runs where id = :'b') = 'queued', 'counted>0 → 自动 queued');
select pg_temp.ok((select max_rows from public.sel_runs where id = :'b') = 2500, '自动 lim = total_count（≤5000）');
select pg_temp.ok((select count(*) from public.sel_batches where run_id = :'b' and batch_no = 1) = 1, '自动建第 1 批');
select pg_temp.ok((select offset_end from public.sel_batches where run_id = :'b' and batch_no = 1) = 1000, '第 1 批 end=min(lim,batch_size)=1000');
select pg_temp.ok((select status from public.sel_runs where id = :'a') = 'running', '已有 running 不挡新任务入队');

-- 2) 超过 5000 → lim=5000
select public.sel_count('gift') as c \gset
update public.sel_runs set status = 'counting' where id = :'c';
update public.sel_runs set status = 'counted', total_count = 19893 where id = :'c';
select pg_temp.ok((select max_rows from public.sel_runs where id = :'c') = 5000, '自动 lim = min(total,5000)');

-- 3) 0 条不入队
select public.sel_count('no results') as z \gset
update public.sel_runs set status = 'counting' where id = :'z';
update public.sel_runs set status = 'counted', total_count = 0 where id = :'z';
select pg_temp.ok((select status from public.sel_runs where id = :'z') = 'counted', 'total_count=0 保持 counted');
select pg_temp.ok((select count(*) from public.sel_batches where run_id = :'z') = 0, '0 条不建批次');

-- 4) 手动 sel_start 在已有 queued/running 时也不报「已有进行中的任务」
select public.sel_count('plate') as d \gset
update public.sel_runs set status = 'counting' where id = :'d';
-- 先关掉触发器路径：直接保持 counted 再手动 start（模拟触发失败后的兜底）
alter table public.sel_runs disable trigger sel_runs_auto_start;
update public.sel_runs set status = 'counted', total_count = 80 where id = :'d';
alter table public.sel_runs enable trigger sel_runs_auto_start;
select pg_temp.ok(public.sel_start(:'d'::uuid, 50), '手动 sel_start 在已有 running/queued 时成功');
select pg_temp.ok((select status from public.sel_runs where id = :'d') = 'queued', '手动后 queued');
select pg_temp.ok((select max_rows from public.sel_runs where id = :'d') = 50, '手动 lim=50');

-- 5) 取消仍可停
select pg_temp.ok(public.sel_cancel(:'b'::uuid), '取消自动入队的任务');
select pg_temp.ok((select status from public.sel_runs where id = :'b') = 'canceled', '取消后 canceled');
select pg_temp.ok((select status from public.sel_batches where run_id = :'b' and batch_no = 1) = 'canceled', '批次一并取消');

-- 6) 静态：sel_start / core 不再 raise 拦截并发（037/052）
select pg_temp.ok(pg_get_functiondef('public.sel_start(uuid,int)'::regprocedure) !~* 'raise exception.*进行中', 'sel_start 无并发 raise');
select pg_temp.ok(pg_get_functiondef('public.sel_start_core(uuid,int)'::regprocedure) !~* 'raise exception.*进行中', 'sel_start_core 无并发 raise');

select 'ALL 052 TESTS PASSED';
