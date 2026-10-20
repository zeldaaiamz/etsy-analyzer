-- 043：获取链接「关键词查询记录」——永久保留，不可删除（Hongyan 2026-10-20）。未在生产执行，需 Hongyan 批准后才上线。
-- 编号：036 留给 ECS 计数 worker。
-- 设计：
--   ① 每次在获取链接点「查询数量」（sel_count 在 sel_runs 插入一行）→ AFTER INSERT 触发器在 sel_keyword_log 插 1 行。
--      不改 sel_count、不改网页提交路径；同一事务内单条 insert，几乎零开销。worker / 旧网页任何插入 sel_runs 的路径都会记录。
--   ② 网页提交前调用一次 sel_keyword_seen(关键词)（主键索引查询）→ 返回以前的查询时间（新的在前），提示「该关键词在X月X日已经查询过」，用户可继续。
--   ③ 永久：不引用 sel_runs / auth.users（无外键 → 不会被 on delete cascade、3 天清理 sel_purge_older_than、逐条删除 sel_delete_run 波及）；
--      BEFORE UPDATE/DELETE 行触发器 + BEFORE TRUNCATE 语句触发器一律报错；对 public/anon/authenticated/service_role 收回 insert/update/delete/truncate；
--      可选的事件触发器阻止 DROP TABLE（权限不足时跳过，不影响迁移）。
-- 规则（见 AGENTS.md）：任何迁移都不得 DROP / TRUNCATE / DELETE 此表、不得禁用其保护触发器；本文件可重复执行（if not exists / create or replace）。
create table if not exists public.sel_keyword_log (
  id bigint generated always as identity primary key,
  user_id uuid not null,                      -- 刻意不加外键（避免删用户级联删除）
  keyword text not null,
  keyword_norm text generated always as (lower(btrim(regexp_replace(keyword, '\s+', ' ', 'g')))) stored,
  queried_at timestamptz not null default now(),
  run_id uuid,                                -- 当时的 sel_runs.id，仅供参考；刻意不加外键
  source text not null default 'sel_count'    -- sel_count | backfill_sel_runs | backfill_batches
);
create index if not exists sel_keyword_log_lookup on public.sel_keyword_log (user_id, keyword_norm, queried_at desc);
create unique index if not exists sel_keyword_log_run_uq on public.sel_keyword_log (run_id) where run_id is not null; -- 回填可重复执行
comment on table public.sel_keyword_log is '获取链接关键词查询记录：永久保留，禁止 DELETE/UPDATE/TRUNCATE/DROP（043，见 AGENTS.md）';

-- 保护：任何角色（包括 service_role / postgres）删改都报错
create or replace function public.sel_keyword_log_guard() returns trigger language plpgsql as $$
begin raise exception 'sel_keyword_log 是永久记录，禁止 %（043）', tg_op using errcode = '42501'; end $$;
drop trigger if exists sel_keyword_log_no_change on public.sel_keyword_log;  -- 只重建触发器本身，不动表
create trigger sel_keyword_log_no_change before update or delete on public.sel_keyword_log for each row execute function public.sel_keyword_log_guard();
drop trigger if exists sel_keyword_log_no_truncate on public.sel_keyword_log;
create trigger sel_keyword_log_no_truncate before truncate on public.sel_keyword_log for each statement execute function public.sel_keyword_log_guard();
alter table public.sel_keyword_log enable always trigger sel_keyword_log_no_change;   -- session_replication_role=replica 时也生效
alter table public.sel_keyword_log enable always trigger sel_keyword_log_no_truncate;

alter table public.sel_keyword_log enable row level security;
revoke all on public.sel_keyword_log from public, anon, authenticated, service_role;
grant select on public.sel_keyword_log to authenticated, service_role;
drop policy if exists sel_keyword_log_read_own on public.sel_keyword_log;
create policy sel_keyword_log_read_own on public.sel_keyword_log for select to authenticated using (user_id = (select auth.uid()));

-- 写入：sel_runs 新增一行即记录（security definer，网页无直接 insert 权限）
create or replace function public.sel_keyword_log_on_run() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.sel_keyword_log (user_id, keyword, queried_at, run_id, source)
  values (new.user_id, new.keyword, coalesce(new.created_at, now()), new.id, 'sel_count')
  on conflict do nothing;
  return null;
end $$;
revoke all on function public.sel_keyword_log_on_run() from public, anon, authenticated;
drop trigger if exists sel_runs_keyword_log on public.sel_runs;
create trigger sel_runs_keyword_log after insert on public.sel_runs for each row execute function public.sel_keyword_log_on_run();

-- 提交前检查：本人此前查询该关键词的时间（新的在前，最多 20 个）
create or replace function public.sel_keyword_seen(p_keyword text) returns timestamptz[]
language sql stable security definer set search_path = public as $$
  select coalesce(array_agg(queried_at order by queried_at desc), '{}') from (
    select queried_at from public.sel_keyword_log
     where user_id = (select auth.uid()) and keyword_norm = lower(btrim(regexp_replace(coalesce(p_keyword, ''), '\s+', ' ', 'g')))
     order by queried_at desc limit 20) t;
$$;
revoke all on function public.sel_keyword_seen(text) from public, anon;
grant execute on function public.sel_keyword_seen(text) to authenticated;

-- 可选：阻止 DROP TABLE（Supabase 需有事件触发器权限；没有就跳过）
create or replace function public.sel_keyword_log_no_drop() returns event_trigger language plpgsql as $$
declare o record;
begin
  for o in select * from pg_event_trigger_dropped_objects() loop
    if o.object_type = 'table' and o.schema_name = 'public' and o.object_name = 'sel_keyword_log' then
      raise exception '禁止 DROP sel_keyword_log（永久记录，043）'; end if;
  end loop;
end $$;
do $$ begin
  if not exists (select 1 from pg_event_trigger where evtname = 'sel_keyword_log_no_drop') then
    create event trigger sel_keyword_log_no_drop on sql_drop execute function public.sel_keyword_log_no_drop();
  end if;
exception when insufficient_privilege then raise notice '043：无事件触发器权限，跳过 DROP 保护（表/行保护仍生效）';
end $$;
