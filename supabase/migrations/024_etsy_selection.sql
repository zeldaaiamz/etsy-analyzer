-- 024：Etsy 选品筛选（选品雷达模块，纯追加：新表 + 新函数，不改动已有表/函数）
-- 数据流（按需）：用户在网站输入关键词点“开始” → sel_start() 建 sel_runs 任务(queued)
--        → ECS 上 selection worker（systemd，与 etsy-analyzer-worker 同模式）sel_claim_run() 认领 → Playwright 已登录 EHunt 的浏览器读列表页（读页面，不用 EHunt API）
--        → 每读到一条：写 sel_raw_rows（原始全保留）+ upsert sel_items(item_state=scraped) → 立即规则+视觉×3 分类 → item_state=classified，按结论设 gate
--        → 前端 #/selscreen 轮询/Realtime 看到逐条进入（进度条）→ 人工复核 → 只有 gate=passed 的进入 #/select。
--        （tools/etsy_selection/import_run.py 仅用于离线回放导入）
--        → 前端 #/selscreen「选品预筛」人工复核（通过 / 排除 / 恢复）→ 只有 gate='passed' 的进入 #/select「选品筛选」→ 一键加入产品库“备选”。
-- 闸门 gate：保留→passed(auto)，待确认→pending（必须人工确认才通过），排除→rejected(auto)；人工可改任意一条（gate_by='user'）。

create table if not exists public.sel_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  keyword text not null,
  source text not null default 'ehunt_list_page',
  params jsonb not null default '{}'::jsonb,      -- limit/价格区间/最低销量/mode/runs/optional_custom/model
  stats jsonb not null default '{}'::jsonb,       -- 计数、成本、tokens、耗时
  created_at timestamptz not null default now(),
  -- 任务队列（按需抓取）
  status text not null default 'queued' check (status in ('queued','running','succeeded','failed','canceled')),
  target_count int not null default 100,          -- 本次最多读多少条
  scraped_count int not null default 0,
  classified_count int not null default 0,
  error text,
  attempts int not null default 0,
  locked_by text, locked_at timestamptz,
  started_at timestamptz, finished_at timestamptz,
  heartbeat_at timestamptz                        -- worker 每条更新；前端据此显示“抓取中/卡住”
);
create index if not exists sel_runs_queue_idx on public.sel_runs (status, created_at) where status in ('queued','running');
create index if not exists sel_runs_user_idx on public.sel_runs (user_id, created_at desc);

-- 原始抓取行全部保留（同一 listing_id 的重复/变体行都在这里）
create table if not exists public.sel_raw_rows (
  id bigserial primary key,
  run_id uuid not null references public.sel_runs(id) on delete cascade,
  rank int,
  etsy_listing_id bigint not null,
  raw jsonb not null
);
create index if not exists sel_raw_rows_run_idx on public.sel_raw_rows (run_id, rank);

-- 最终表：每个 run 每个 listing_id 一行
create table if not exists public.sel_items (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.sel_runs(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  etsy_listing_id bigint not null,
  ranks int[] not null default '{}',
  title text,
  price_min numeric, price_max numeric,
  total_sales int, sales_7d int,
  sales_ratio numeric generated always as (case when coalesce(total_sales,0) > 0 then round(sales_7d::numeric / total_sales, 4) end) stored,
  image_url text,                                  -- il_794xN
  verdict text not null check (verdict in ('保留','待确认','排除')),
  reason text, note text, explain text, source text,  -- source: 规则 / 视觉
  material text,
  acrylic_fit text check (acrylic_fit is null or acrylic_fit in ('是','可改造','不适合','')),
  custom_type text,                                -- 无 / 可选定制 / 必须定制 / 待确认
  selling_points text[] not null default '{}',
  occasions text[] not null default '{}',
  votes text, dup_note text,
  item_state text not null default 'scraped' check (item_state in ('scraped','classifying','classified','error')),  -- 逐条流水
  scraped_at timestamptz not null default now(), classified_at timestamptz,
  gate text not null default 'pending' check (gate in ('pending','passed','rejected')),   -- 预筛选闸门
  gate_by text not null default 'auto' check (gate_by in ('auto','user')),
  gate_at timestamptz,
  created_at timestamptz not null default now(),
  unique (run_id, etsy_listing_id)
);
create index if not exists sel_items_run_gate_idx on public.sel_items (run_id, gate);
create index if not exists sel_items_user_listing_idx on public.sel_items (user_id, etsy_listing_id);

alter table public.sel_runs enable row level security;
alter table public.sel_raw_rows enable row level security;
alter table public.sel_items enable row level security;
create policy sel_runs_select on public.sel_runs for select to authenticated using (user_id = (select auth.uid()));
create policy sel_items_select on public.sel_items for select to authenticated using (user_id = (select auth.uid()));
create policy sel_raw_select on public.sel_raw_rows for select to authenticated
  using (exists (select 1 from public.sel_runs r where r.id = run_id and r.user_id = (select auth.uid())));
revoke all on public.sel_runs, public.sel_raw_rows, public.sel_items from anon;
revoke insert, update, delete on public.sel_runs, public.sel_raw_rows, public.sel_items from authenticated;  -- 写入只走 service_role 导入脚本

-- 按需开始：用户建任务（同一用户最多 1 个进行中的任务）
create or replace function public.sel_start(p_keyword text, p_params jsonb default '{}'::jsonb)
returns uuid language plpgsql security definer set search_path = public as $$
declare rid uuid; uid uuid := (select auth.uid());
begin
  if uid is null or coalesce(trim(p_keyword), '') = '' then raise exception '需要登录并填写关键词'; end if;
  if exists (select 1 from public.sel_runs where user_id = uid and status in ('queued','running')) then raise exception '已有进行中的选品任务'; end if;
  insert into public.sel_runs (user_id, keyword, params, target_count)
  values (uid, trim(p_keyword), coalesce(p_params, '{}'::jsonb) || jsonb_build_object('mode','convertible','runs',3,'optional_custom','keep'),
          least(greatest(coalesce((p_params->>'limit')::int, 100), 1), 300))
  returning id into rid;
  return rid;
end $$;
revoke all on function public.sel_start(text, jsonb) from public, anon;
grant execute on function public.sel_start(text, jsonb) to authenticated;

create or replace function public.sel_cancel(p_run uuid) returns boolean language sql security definer set search_path = public as $$
  update public.sel_runs set status = 'canceled', finished_at = now() where id = p_run and user_id = auth.uid() and status in ('queued','running') returning true;
$$;
revoke all on function public.sel_cancel(uuid) from public, anon;
grant execute on function public.sel_cancel(uuid) to authenticated;

-- worker 认领（仅 service_role）：queued，或 running 但心跳超时 10 分钟（worker 崩溃回收）
create or replace function public.sel_claim_run(p_worker text) returns public.sel_runs
language plpgsql security definer set search_path = public as $$
declare r public.sel_runs;
begin
  select * into r from public.sel_runs
   where status = 'queued' or (status = 'running' and coalesce(heartbeat_at, locked_at) < now() - interval '10 minutes' and attempts < 3)
   order by created_at for update skip locked limit 1;
  if not found then return null; end if;
  update public.sel_runs set status = 'running', locked_by = p_worker, locked_at = now(), heartbeat_at = now(),
         started_at = coalesce(started_at, now()), attempts = attempts + 1
   where id = r.id returning * into r;
  return r;
end $$;
revoke all on function public.sel_claim_run(text) from public, anon, authenticated;
grant execute on function public.sel_claim_run(text) to service_role;

-- Realtime：前端订阅逐条进入（也可退回 3 秒轮询）
do $$ begin
  begin alter publication supabase_realtime add table public.sel_runs; exception when duplicate_object then null; end;
  begin alter publication supabase_realtime add table public.sel_items; exception when duplicate_object then null; end;
end $$;

-- 预筛选复核：人工改闸门（passed / rejected / pending=恢复为待确认）；只能改本人的
create or replace function public.sel_set_gate(p_items uuid[], p_gate text)
returns int language plpgsql security definer set search_path = public as $$
declare n int;
begin
  if (select auth.uid()) is null or p_gate not in ('pending','passed','rejected') then return 0; end if;
  update public.sel_items set gate = p_gate, gate_by = 'user', gate_at = now()
   where id = any(p_items) and user_id = (select auth.uid()) and item_state = 'classified';
  get diagnostics n = row_count; return n;
end $$;
revoke all on function public.sel_set_gate(uuid[], text) from public, anon;
grant execute on function public.sel_set_gate(uuid[], text) to authenticated;

-- 列表：某 run 的最终行 + 跨关键词计数（本人所有 run 中出现该 listing 的不同关键词数）+ 是否已在产品库
-- p_stage: 'screen' = 预筛选（全部行）；'select' = 选品筛选（只返回 gate='passed'）
create or replace function public.sel_list(p_run uuid, p_stage text default 'select')
returns jsonb language sql stable security invoker set search_path = public as $$
  select coalesce(jsonb_agg(to_jsonb(i) || jsonb_build_object(
           'cross_kw', (select count(distinct r2.keyword) from public.sel_items i2 join public.sel_runs r2 on r2.id = i2.run_id
                         where i2.user_id = i.user_id and i2.etsy_listing_id = i.etsy_listing_id),
           'cross_kw_list', (select jsonb_agg(distinct r2.keyword) from public.sel_items i2 join public.sel_runs r2 on r2.id = i2.run_id
                         where i2.user_id = i.user_id and i2.etsy_listing_id = i.etsy_listing_id),
           'in_library', exists (select 1 from public.product_library l where l.user_id = i.user_id and l.etsy_listing_id = i.etsy_listing_id))
         order by i.ranks[1] nulls last), '[]'::jsonb)
    from public.sel_items i where i.run_id = p_run and i.user_id = auth.uid()
     and (coalesce(p_stage, 'select') = 'screen' or (i.gate = 'passed' and i.item_state = 'classified'));
$$;
revoke all on function public.sel_list(uuid, text) from public, anon;
grant execute on function public.sel_list(uuid, text) to authenticated;

-- 一键加入产品库（备选 = stage 'candidate'）；已存在则不覆盖
create or replace function public.sel_add_to_library(p_items uuid[])
returns int language plpgsql security definer set search_path = public as $$
declare n int;
begin
  if (select auth.uid()) is null then return 0; end if;
  insert into public.product_library (user_id, job_id, etsy_listing_id, etsy_url, title, image_url, price, currency, product_summary, keywords, stage, note)
  select distinct on (i.etsy_listing_id) i.user_id, null, i.etsy_listing_id, 'https://www.etsy.com/listing/' || i.etsy_listing_id, i.title, i.image_url,
         i.price_min, 'USD', array_to_string(i.selling_points, '；'), i.occasions, 'candidate',
         concat_ws(' · ', '选品筛选：' || r.keyword, nullif(i.note, ''), 'acrylic_fit=' || coalesce(i.acrylic_fit, ''))
    from public.sel_items i join public.sel_runs r on r.id = i.run_id
   where i.id = any(p_items) and i.user_id = (select auth.uid()) and i.gate = 'passed'   -- 未通过预筛选的不能加入
   order by i.etsy_listing_id, i.created_at desc
  on conflict (user_id, etsy_listing_id) do nothing;
  get diagnostics n = row_count; return n;
end $$;
revoke all on function public.sel_add_to_library(uuid[]) from public, anon;
grant execute on function public.sel_add_to_library(uuid[]) to authenticated;
