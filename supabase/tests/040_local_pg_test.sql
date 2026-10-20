-- 040 本地测试（自带最小桩：jobs 只含相关列）
create role anon; create role authenticated;
create schema auth; create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
create table public.jobs (id uuid primary key default gen_random_uuid(), user_id uuid not null, batch_id uuid, prescreen boolean not null default true, status text not null, screen_state text, screen_hidden_at timestamptz);
create function public.screen_unhide(p_ids uuid[] default null) returns int language sql security definer set search_path = public as $$
  with u as (update public.jobs set screen_hidden_at = null where user_id = auth.uid() and prescreen and screen_hidden_at is not null and (p_ids is null or id = any(p_ids)) returning 1) select count(*)::int from u; $$;
\i migrations/040_screen_hide_ids.sql
\set u1 '11111111-1111-1111-1111-111111111111'
\set u2 '22222222-2222-2222-2222-222222222222'
insert into public.jobs (id, user_id, batch_id, status, screen_state) values
 ('00000000-0000-0000-0000-000000000001', :'u1', gen_random_uuid(), 'screening', 'awaiting'),
 ('00000000-0000-0000-0000-000000000002', :'u1', gen_random_uuid(), 'failed', null),
 ('00000000-0000-0000-0000-000000000003', :'u1', gen_random_uuid(), 'canceled', 'rejected'),
 ('00000000-0000-0000-0000-000000000004', :'u1', gen_random_uuid(), 'queued', null),
 ('00000000-0000-0000-0000-000000000005', :'u1', gen_random_uuid(), 'running', null),
 ('00000000-0000-0000-0000-000000000006', :'u1', gen_random_uuid(), 'succeeded', 'kept'),
 ('00000000-0000-0000-0000-000000000007', :'u2', gen_random_uuid(), 'screening', 'awaiting');
create function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;
select pg_temp.ok((public.screen_hide_ids(array['00000000-0000-0000-0000-000000000001'::uuid])->>'hidden')::int = 0, '未登录：不隐藏任何条目');
select set_config('request.jwt.claim.sub', :'u1', false);
select pg_temp.ok((public.screen_hide_ids(array(select id from public.jobs))->>'hidden')::int = 3, '一次调用跨批次隐藏 3 条（待确认/失败/已排除）');
select pg_temp.ok((select count(*) from public.jobs where screen_hidden_at is not null) = 3, '排队/运行/已进入分析/别人的都没动');
select pg_temp.ok((public.screen_hide_ids(array(select id from public.jobs))->>'hidden')::int = 0, '重复提交（连点）不重复生效');
select pg_temp.ok((select count(*) from public.jobs) = 7, '没有删除任何行');
select pg_temp.ok(public.screen_unhide(array['00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000003']::uuid[]) = 3, '撤销：screen_unhide 恢复 3 条');
