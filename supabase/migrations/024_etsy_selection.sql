-- 024：Etsy 选品筛选（选品雷达模块，纯追加：新表 + 新函数，不改动已有表/函数）
-- 数据流：浏览器读 EHunt 列表页（人工/独立采集，不用 EHunt API）→ JSON → tools/etsy_selection/classify.py（规则 + Doubao 视觉×3 投票）
--        → tools/etsy_selection/import_run.py（service_role）写入 sel_runs / sel_raw_rows / sel_items → 前端 #/select 只读展示，一键加入产品库“备选”。

create table if not exists public.sel_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  keyword text not null,
  source text not null default 'ehunt_list_page',
  params jsonb not null default '{}'::jsonb,      -- mode/runs/optional_custom/model/价格区间等
  stats jsonb not null default '{}'::jsonb,       -- 计数、成本、tokens、耗时
  created_at timestamptz not null default now()
);
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
  created_at timestamptz not null default now(),
  unique (run_id, etsy_listing_id)
);
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

-- 列表：某 run 的最终行 + 跨关键词计数（本人所有 run 中出现该 listing 的不同关键词数）+ 是否已在产品库
create or replace function public.sel_list(p_run uuid)
returns jsonb language sql stable security invoker set search_path = public as $$
  select coalesce(jsonb_agg(to_jsonb(i) || jsonb_build_object(
           'cross_kw', (select count(distinct r2.keyword) from public.sel_items i2 join public.sel_runs r2 on r2.id = i2.run_id
                         where i2.user_id = i.user_id and i2.etsy_listing_id = i.etsy_listing_id),
           'cross_kw_list', (select jsonb_agg(distinct r2.keyword) from public.sel_items i2 join public.sel_runs r2 on r2.id = i2.run_id
                         where i2.user_id = i.user_id and i2.etsy_listing_id = i.etsy_listing_id),
           'in_library', exists (select 1 from public.product_library l where l.user_id = i.user_id and l.etsy_listing_id = i.etsy_listing_id))
         order by i.ranks[1] nulls last), '[]'::jsonb)
    from public.sel_items i where i.run_id = p_run and i.user_id = auth.uid();
$$;
revoke all on function public.sel_list(uuid) from public, anon;
grant execute on function public.sel_list(uuid) to authenticated;

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
   where i.id = any(p_items) and i.user_id = (select auth.uid())
   order by i.etsy_listing_id, i.created_at desc
  on conflict (user_id, etsy_listing_id) do nothing;
  get diagnostics n = row_count; return n;
end $$;
revoke all on function public.sel_add_to_library(uuid[]) from public, anon;
grant execute on function public.sel_add_to_library(uuid[]) to authenticated;
