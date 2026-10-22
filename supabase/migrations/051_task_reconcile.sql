-- 051 预筛选对账 + 我的任务按确认时间排序（2026-10-22 Hongyan：book 预筛选通过 44 条，我的任务只看到 30 条）
-- 根因（只读核对，无数据丢失）：我的任务按 jobs.created_at（发送时间）倒序、首屏 30 条 → 今天确认、10-21 发送的 44 条排在第 31–74 行；
-- 加入产品库的条目按设计只在产品库显示。本迁移：
--   ① task_list 按 coalesce(screened_at, created_at) 倒序（刚确认的排最前），并返回 total（共 N 个任务）；
--   ② 表达式索引支撑新排序；
--   ③ screen_reconcile()：只读对账，每个预筛选批次：确认 N · 已进任务 M（第几行）· 产品库 L · 缺 K，列出每条与原因；已删除（隐藏）条目另列，可 screen_unhide 撤销。
-- 只读函数 + 索引 + 替换 task_list；不改表结构、不动 sel_keyword_log。
create index if not exists jobs_user_sort_idx on public.jobs (user_id, (coalesce(screened_at, created_at)) desc);

create or replace function public.task_list(p_limit integer default 30, p_ids uuid[] default null::uuid[])
returns jsonb language sql stable set search_path to 'public' as $function$
  with page as (
    select j.* from public.task_jobs j
     where p_ids is null or j.id = any(p_ids)
     order by coalesce(j.screened_at, j.created_at) desc, j.id
     limit case when p_ids is null then least(greatest(coalesce(p_limit, 30), 1), 500) + 1 else 1000 end)
  select jsonb_build_object(
    'jobs', coalesce((select jsonb_agg(row order by (row->>'sort_at')::timestamptz desc, row->>'id') from (
      select to_jsonb(p) - 'locked_by' - 'locked_at' - 'run_after' - 'batch_id'
        || jsonb_build_object(
          'sort_at', coalesce(p.screened_at, p.created_at),
          'e', (select jsonb_strip_nulls(jsonb_build_object('title', e.title, 'image_url', e.image_url, 'price', e.price,
                  'shop_name', e.shop_name, 'listing_id', e.listing_id,
                  'raw', case when e.raw ? 'ehunt' then jsonb_build_object('ehunt', jsonb_strip_nulls(jsonb_build_object(
                    'sales_weekly', e.raw->'ehunt'->'sales_weekly', 'sales_total', e.raw->'ehunt'->'sales_total',
                    'favorites', e.raw->'ehunt'->'favorites', 'favorites_weekly', e.raw->'ehunt'->'favorites_weekly',
                    'reviews', e.raw->'ehunt'->'reviews'))) end))
                from public.etsy_products e where e.job_id = p.id),
          'a', (select jsonb_build_object('product_summary', a.product_summary, 'target_audience', a.target_audience,
                  'keywords', a.keywords, 'has_trace', case when jsonb_typeof(a.decision_trace) = 'array' then jsonb_array_length(a.decision_trace) > 0 else false end,
                  'audience_evidence', case when a.audience_evidence is null then null
                    else (a.audience_evidence - 'evidence') || jsonb_build_object('evidence', jsonb_path_query_array(a.audience_evidence, '$.evidence[0 to 3]')) end)
                from public.analyses a where a.job_id = p.id),
          'n', (select jsonb_build_object('identical', count(*) filter (where s.kind = 'identical'),
                  'similar', count(*) filter (where s.kind = 'similar'), 'search_result', count(*) filter (where s.kind = 'search_result'))
                from public.evidence_snapshots s where s.job_id = p.id),
          'asins', (select coalesce(jsonb_agg(distinct upper(x.asin)), '[]'::jsonb) from (
                  select asin from public.evidence_snapshots where job_id = p.id and asin is not null
                  union select asin from public.amazon_matches where job_id = p.id and asin is not null) x)
        ) as row
      from (select * from page order by coalesce(screened_at, created_at) desc, id limit case when p_ids is null then least(greatest(coalesce(p_limit, 30), 1), 500) else 1000 end) p) r), '[]'::jsonb),
    'has_more', p_ids is null and (select count(*) from page) > least(greatest(coalesce(p_limit, 30), 1), 500),
    'total', (select count(*) from public.task_jobs j where j.user_id = auth.uid()),
    'lib_count', (select count(*) from public.product_library l where l.user_id = auth.uid()));
$function$;

-- 对账：每个预筛选批次 → 确认 N · 已进任务 M · 产品库 L · 缺 K；items 每条带原因与在我的任务中的行号（按新排序）；deleted 为已删除（隐藏）未确认条目
create or replace function public.screen_reconcile(p_batch uuid default null)
returns jsonb language sql stable set search_path to 'public' as $function$
  with t as (select j.id, row_number() over (order by coalesce(j.screened_at, j.created_at) desc, j.id) rn
               from public.task_jobs j where j.user_id = auth.uid()),
  x as (
    select j.id, j.batch_id, j.etsy_listing_id, j.status::text status, j.screen_state, j.screen_hidden_at, j.error, j.screened_at, j.created_at,
           e.title, e.image_url, t.rn,
           exists (select 1 from public.product_library l where l.user_id = j.user_id and l.etsy_listing_id = j.etsy_listing_id) in_lib
      from public.jobs j left join public.etsy_products e on e.job_id = j.id left join t on t.id = j.id
     where j.user_id = auth.uid() and j.prescreen and (p_batch is null or j.batch_id = p_batch)),
  k as (select x.*, case when screen_state is distinct from 'kept' then null
                         when rn is not null and status = 'failed' then 'failed'
                         when rn is not null then 'task'
                         when in_lib then 'library'
                         else 'missing' end reason from x)
  select coalesce(jsonb_agg(jsonb_build_object(
           'batch_id', b.id, 'name', b.name, 'created_at', b.created_at,
           'confirmed', s.confirmed, 'in_tasks', s.in_tasks, 'in_library', s.in_library, 'failed', s.failed, 'missing', s.missing, 'deleted', s.deleted,
           'items', s.items, 'deleted_items', s.deleted_items) order by s.last_at desc nulls last), '[]'::jsonb)
    from (select batch_id,
                 count(*) filter (where screen_state = 'kept') confirmed,
                 count(*) filter (where reason in ('task', 'failed')) in_tasks,
                 count(*) filter (where reason = 'library') in_library,
                 count(*) filter (where reason = 'failed') failed,
                 count(*) filter (where reason = 'missing') missing,
                 count(*) filter (where screen_state is distinct from 'kept' and screen_hidden_at is not null) deleted,
                 max(coalesce(screened_at, created_at)) last_at,
                 coalesce(jsonb_agg(jsonb_build_object('id', id, 'listing_id', etsy_listing_id, 'title', title, 'image_url', image_url, 'status', status,
                   'reason', reason, 'task_row', rn, 'error', case when reason = 'failed' then left(error, 160) end) order by rn nulls last, etsy_listing_id)
                   filter (where screen_state = 'kept'), '[]'::jsonb) items,
                 coalesce(jsonb_agg(jsonb_build_object('id', id, 'listing_id', etsy_listing_id, 'title', title, 'image_url', image_url, 'hidden_at', screen_hidden_at)
                   order by screen_hidden_at desc, etsy_listing_id)
                   filter (where screen_state is distinct from 'kept' and screen_hidden_at is not null), '[]'::jsonb) deleted_items
            from k group by batch_id) s
    join public.batches b on b.id = s.batch_id
   where s.confirmed > 0 or s.deleted > 0;
$function$;
revoke all on function public.screen_reconcile(uuid) from public, anon;
grant execute on function public.screen_reconcile(uuid) to authenticated;
