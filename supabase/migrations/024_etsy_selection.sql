-- 024：Etsy「获取链接」（选品雷达模块）。纯追加：新表 + 新函数，不改动已有表/函数。未在生产执行。
-- 定稿（Hongyan 2026-10-16）：
--   1 关键词 → sel_count() 建轻量任务 → box worker 在 EHunt 界面设筛选（价格 8–40、总销量 ≥100、7 天销量 >3、按 7 天销量降序）只读 “Find N products”
--   2 用户填最多读取条数 → sel_start()；>1000 时分批，每批 1000，最多 5 批 / 5,000（EHunt 只显示前 5,000）；sel_next_batch(run, all)
--   3 box worker 逐行读列表 → 标题规则先筛（排除的不调豆包、不入库，原因只写 box 本地日志 3 天）→ 通过的才调豆包视觉
--   4 只有符合要求的（保留 + 待确认）写入 sel_items；被排除的链接不入库，只在 sel_runs/sel_batches 记汇总数量
--   5 数据只保留 3 天（按批次完成时间），sel_purge_older_than() + pg_cron 每日清理，不归档
--   6 网页无搜索；查询由 序猿 在对话里用 tools/etsy_selection/lookup.py（service_role）完成

create table if not exists public.sel_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  keyword text not null,
  source text not null default 'ehunt_list_page',
  params jsonb not null default '{}'::jsonb,
  status text not null default 'count_queued'
    check (status in ('count_queued','counting','counted','queued','running','batch_done','succeeded','failed','canceled')),
  total_count int, counted_at timestamptz,
  batch_size int not null default 1000,
  max_rows int,
  current_batch int not null default 0,
  remaining_count int,                            -- 还剩未读：只在每批完成时更新
  read_all boolean not null default false,
  scraped_count int not null default 0,           -- 汇总数（无逐条数据）
  passed_count int not null default 0,
  excluded_count int not null default 0,
  excluded_by_reason jsonb not null default '{}'::jsonb,
  vision_calls int not null default 0,
  error text, attempts int not null default 0,
  locked_by text, locked_at timestamptz, heartbeat_at timestamptz,
  created_at timestamptz not null default now(), started_at timestamptz, finished_at timestamptz
);
create index if not exists sel_runs_queue_idx on public.sel_runs (status, created_at) where status in ('count_queued','counting','queued','running');
create index if not exists sel_runs_user_idx on public.sel_runs (user_id, created_at desc);

create table if not exists public.sel_batches (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.sel_runs(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  batch_no int not null check (batch_no between 1 and 5),
  offset_start int not null, offset_end int not null,   -- EHunt 排名 1-based 闭区间
  page_start int not null, page_end int not null,       -- 每页 20 条；worker 从 page_start 续读
  last_page int,
  status text not null default 'queued' check (status in ('queued','running','done','failed','canceled')),
  scraped_count int not null default 0, passed_count int not null default 0, excluded_count int not null default 0,
  dup_count int not null default 0,
  excluded_by_reason jsonb not null default '{}'::jsonb,
  remaining_after int,
  copied_at timestamptz,
  created_at timestamptz not null default now(), started_at timestamptz, finished_at timestamptz,
  unique (run_id, batch_no)
);
create index if not exists sel_batches_finished_idx on public.sel_batches (finished_at);

-- 只存符合要求的链接（保留 + 待确认）。每个 run 每个 listing 一行（跨批去重）。
create table if not exists public.sel_items (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.sel_runs(id) on delete cascade,
  batch_id uuid references public.sel_batches(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  batch_no int not null default 1,
  etsy_listing_id bigint not null,
  etsy_url text generated always as ('https://www.etsy.com/listing/' || etsy_listing_id) stored,
  rank int, page int,
  title text, price_min numeric, price_max numeric, total_sales int, sales_7d int,
  image_url text,
  verdict text not null check (verdict in ('保留','待确认')),
  reason text, note text, explain text,
  material text, acrylic_fit text, custom_type text,
  selling_points text[] not null default '{}', occasions text[] not null default '{}', votes text, dup_note text,
  created_at timestamptz not null default now(),
  unique (run_id, etsy_listing_id)
);
create index if not exists sel_items_user_listing_idx on public.sel_items (user_id, etsy_listing_id);
create index if not exists sel_items_batch_idx on public.sel_items (batch_id);

alter table public.sel_runs enable row level security;
alter table public.sel_batches enable row level security;
alter table public.sel_items enable row level security;
create policy sel_runs_select on public.sel_runs for select to authenticated using (user_id = (select auth.uid()));
create policy sel_batches_select on public.sel_batches for select to authenticated using (user_id = (select auth.uid()));
create policy sel_items_select on public.sel_items for select to authenticated using (user_id = (select auth.uid()));
revoke all on public.sel_runs, public.sel_batches, public.sel_items from anon;
revoke insert, update, delete on public.sel_runs, public.sel_batches, public.sel_items from authenticated;

create or replace function public.sel_count(p_keyword text, p_params jsonb default '{}'::jsonb)
returns uuid language plpgsql security definer set search_path = public as $$
declare rid uuid; uid uuid := (select auth.uid());
begin
  if uid is null or coalesce(trim(p_keyword), '') = '' then raise exception '需要登录并填写关键词'; end if;
  if exists (select 1 from public.sel_runs where user_id = uid and status in ('count_queued','counting','queued','running')) then raise exception '已有进行中的任务'; end if;
  insert into public.sel_runs (user_id, keyword, status, params)
  values (uid, trim(p_keyword), 'count_queued',
          jsonb_build_object('price_min',8,'price_max',40,'min_sales',100,'min_sales_7d',4,'sort','↓7-day sales') || coalesce(p_params, '{}'::jsonb)
          || jsonb_build_object('mode','convertible','runs',3,'optional_custom','keep'))
  returning id into rid;
  return rid;
end $$;
revoke all on function public.sel_count(text, jsonb) from public, anon;
grant execute on function public.sel_count(text, jsonb) to authenticated;

create or replace function public.sel_start(p_run uuid, p_limit int default 100)
returns boolean language plpgsql security definer set search_path = public as $$
declare r public.sel_runs; lim int; e int;
begin
  select * into r from public.sel_runs where id = p_run and user_id = (select auth.uid()) and status = 'counted' for update;
  if not found then return false; end if;
  lim := least(greatest(coalesce(p_limit, 100), 1), coalesce(r.total_count, 5000), 5000);
  e := least(lim, r.batch_size);
  update public.sel_runs set status = 'queued', max_rows = lim, current_batch = 1, params = params || jsonb_build_object('limit', lim) where id = p_run;
  insert into public.sel_batches (run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end)
  values (p_run, r.user_id, 1, 1, e, 1, (e + 19) / 20);
  return true;
end $$;
revoke all on function public.sel_start(uuid, int) from public, anon;
grant execute on function public.sel_start(uuid, int) to authenticated;

create or replace function public.sel_next_batch(p_run uuid, p_all boolean default false)
returns int language plpgsql security definer set search_path = public as $$
declare r public.sel_runs; nb int; s int; e int;
begin
  select * into r from public.sel_runs where id = p_run and user_id = (select auth.uid()) and status = 'batch_done' for update;
  if not found then return 0; end if;
  nb := r.current_batch + 1; s := r.current_batch * r.batch_size + 1; e := least(nb * r.batch_size, r.max_rows);
  if nb > 5 or s > r.max_rows then return 0; end if;
  insert into public.sel_batches (run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end)
  values (p_run, r.user_id, nb, s, e, (s + 19) / 20, (e + 19) / 20);
  update public.sel_runs set status = 'queued', current_batch = nb, read_all = coalesce(p_all, false) where id = p_run;
  return nb;
end $$;
revoke all on function public.sel_next_batch(uuid, boolean) from public, anon;
grant execute on function public.sel_next_batch(uuid, boolean) to authenticated;

create or replace function public.sel_cancel(p_run uuid) returns boolean language sql security definer set search_path = public as $$
  update public.sel_runs set status = 'canceled', finished_at = now() where id = p_run and user_id = auth.uid()
     and status in ('count_queued','counting','counted','queued','running','batch_done') returning true;
$$;
revoke all on function public.sel_cancel(uuid) from public, anon;
grant execute on function public.sel_cancel(uuid) to authenticated;

create or replace function public.sel_mark_copied(p_batch uuid) returns boolean language sql security definer set search_path = public as $$
  update public.sel_batches set copied_at = now() where id = p_batch and user_id = auth.uid() returning true;
$$;
revoke all on function public.sel_mark_copied(uuid) from public, anon;
grant execute on function public.sel_mark_copied(uuid) to authenticated;

create or replace function public.sel_claim_run(p_worker text) returns public.sel_runs
language plpgsql security definer set search_path = public as $$
declare r public.sel_runs;
begin
  select * into r from public.sel_runs
   where status in ('queued','count_queued') or (status in ('running','counting') and coalesce(heartbeat_at, locked_at) < now() - interval '10 minutes' and attempts < 3)
   order by created_at for update skip locked limit 1;
  if not found then return null; end if;
  update public.sel_runs set status = case when r.status in ('count_queued','counting') then 'counting' else 'running' end,
         locked_by = p_worker, locked_at = now(), heartbeat_at = now(), attempts = attempts + 1,
         started_at = case when r.status in ('count_queued','counting') then started_at else coalesce(started_at, now()) end
   where id = r.id returning * into r;
  return r;
end $$;
revoke all on function public.sel_claim_run(text) from public, anon, authenticated;
grant execute on function public.sel_claim_run(text) to service_role;

-- 保留 3 天：按批次完成时间删除 items + batches；没有批次的过期 run 一并删除。不归档。
create or replace function public.sel_purge_older_than(p_age interval default interval '3 days')
returns jsonb language plpgsql security definer set search_path = public as $$
declare ni int; nb int; nr int;
begin
  delete from public.sel_items i using public.sel_batches b where i.batch_id = b.id and b.finished_at < now() - p_age;
  get diagnostics ni = row_count;
  delete from public.sel_batches where finished_at < now() - p_age;
  get diagnostics nb = row_count;
  delete from public.sel_runs r where coalesce(r.finished_at, r.created_at) < now() - p_age
     and r.status not in ('queued','running','counting','count_queued')
     and not exists (select 1 from public.sel_batches b where b.run_id = r.id);
  get diagnostics nr = row_count;
  return jsonb_build_object('items', ni, 'batches', nb, 'runs', nr);
end $$;
revoke all on function public.sel_purge_older_than(interval) from public, anon, authenticated;
grant execute on function public.sel_purge_older_than(interval) to service_role;

-- 定时：pg_cron 1.6.4 在本项目可用但未安装（2026-10-16 只读检查 list_extensions）。启用后每日 03:17 UTC 清理；
-- 不可用时由 box worker 启动时和每 24 小时调用 sel_purge_older_than 兜底。
do $$ begin
  create extension if not exists pg_cron;
  perform cron.schedule('sel-purge-3d', '17 3 * * *', $c$select public.sel_purge_older_than(interval '3 days')$c$);
exception when others then raise notice 'pg_cron 不可用，改由 box worker 定时清理：%', sqlerrm;
end $$;

do $$ begin
  begin alter publication supabase_realtime add table public.sel_runs; exception when duplicate_object then null; end;
  begin alter publication supabase_realtime add table public.sel_batches; exception when duplicate_object then null; end;
end $$;
