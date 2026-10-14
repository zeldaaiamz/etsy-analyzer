-- 015：性能优化（纯追加 / 向后兼容：旧前端与旧 worker 不受影响）
-- H4  task_list()：任务列表一次 RPC 返回扁平的轻量数据（不含 decision_trace / raw / 快照明细）
-- H2  library_page()：产品库按阶段 / 搜索 / 原因服务端分页，只返回卡片需要的列
-- M3  jobs.etsy_listing_id 由 etsy_products 触发器回填 → task_jobs 视图去掉嵌套子查询
-- L3  batches.user_id 索引；L5 provider_cache 过期清理函数（worker 定期调用）

-- ---------- M3 ----------
create or replace function public.jobs_fill_listing_id() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.listing_id is not null then
    update public.jobs set etsy_listing_id = new.listing_id where id = new.job_id and etsy_listing_id is null;
  end if;
  return new;
end $$;
revoke all on function public.jobs_fill_listing_id() from public, anon, authenticated;
drop trigger if exists etsy_products_fill_job_listing on public.etsy_products;
create trigger etsy_products_fill_job_listing after insert or update of listing_id on public.etsy_products
  for each row execute function public.jobs_fill_listing_id();
update public.jobs j set etsy_listing_id = e.listing_id from public.etsy_products e
 where e.job_id = j.id and j.etsy_listing_id is null and e.listing_id is not null;

create or replace view public.task_jobs with (security_invoker = true) as
select j.* from public.jobs j
 where j.etsy_listing_id is null
    or not exists (select 1 from public.product_library l where l.user_id = j.user_id and l.etsy_listing_id = j.etsy_listing_id);
revoke all on public.task_jobs from anon;
grant select on public.task_jobs to authenticated;

-- ---------- H4 ----------
create or replace function public.task_list(p_limit int default 30, p_ids uuid[] default null)
returns jsonb language sql stable security invoker set search_path = public as $$
  with page as (
    select j.* from public.task_jobs j
     where p_ids is null or j.id = any(p_ids)
     order by j.created_at desc
     limit case when p_ids is null then least(greatest(coalesce(p_limit, 30), 1), 500) + 1 else 1000 end)
  select jsonb_build_object(
    'jobs', coalesce((select jsonb_agg(row order by (row->>'created_at')::timestamptz desc) from (
      select to_jsonb(p) - 'locked_by' - 'locked_at' - 'run_after' - 'batch_id'
        || jsonb_build_object(
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
      from (select * from page order by created_at desc limit case when p_ids is null then least(greatest(coalesce(p_limit, 30), 1), 500) else 1000 end) p) r), '[]'::jsonb),
    'has_more', p_ids is null and (select count(*) from page) > least(greatest(coalesce(p_limit, 30), 1), 500),
    'lib_count', (select count(*) from public.product_library l where l.user_id = auth.uid()));
$$;
revoke all on function public.task_list(int, uuid[]) from public, anon;
grant execute on function public.task_list(int, uuid[]) to authenticated;

-- ---------- H2 ----------
create or replace function public.library_page(p_stage text default 'all', p_q text default null, p_reason text default null,
                                               p_limit int default 60, p_offset int default 0)
returns jsonb language sql stable security invoker set search_path = public as $$
  with mine as (select * from public.product_library where user_id = auth.uid()),
  f as (
    select * from mine l
     where (coalesce(p_stage, 'all') = 'all' or coalesce(l.stage, 'candidate') = p_stage)
       and (p_stage is distinct from 'rejected' or p_reason is null or p_reason = any(l.reject_reasons))
       and (coalesce(p_q, '') = '' or concat_ws(' ', l.title, l.etsy_listing_id::text, l.product_summary, l.target_audience,
             l.shop_name, l.stage_note, array_to_string(l.keywords, ' ')) ilike '%' || replace(replace(replace(p_q, '\', '\\'), '%', '\%'), '_', '\_') || '%')),
  pg as (select * from f order by coalesce(stage_changed_at, added_at) desc, id
          limit least(greatest(coalesce(p_limit, 60), 1), 500) offset greatest(coalesce(p_offset, 0), 0))
  select jsonb_build_object(
    'items', coalesce((select jsonb_agg(jsonb_build_object('id', id, 'job_id', job_id, 'etsy_listing_id', etsy_listing_id, 'etsy_url', etsy_url,
               'title', title, 'image_url', image_url, 'price', price, 'shop_name', shop_name, 'product_summary', product_summary,
               'target_audience', target_audience, 'keywords', keywords, 'identical_count', identical_count, 'similar_count', similar_count,
               'added_at', added_at, 'stage', stage, 'reject_reasons', reject_reasons, 'stage_note', stage_note, 'stage_changed_at', stage_changed_at)
               order by coalesce(stage_changed_at, added_at) desc, id) from pg), '[]'::jsonb),
    'total', (select count(*) from f),
    'counts', (select jsonb_build_object('all', count(*), 'candidate', count(*) filter (where coalesce(stage, 'candidate') = 'candidate'),
               'active', count(*) filter (where stage = 'active'), 'rejected', count(*) filter (where stage = 'rejected')) from mine),
    'reasons', (select coalesce(jsonb_object_agg(k, n), '{}'::jsonb) from (
               select k, count(*) n from mine, unnest(reject_reasons) k where stage = 'rejected' group by k) r),
    'pending', (select coalesce(jsonb_agg(distinct j.etsy_listing_id), '[]'::jsonb) from pg join public.jobs j
               on pg.job_id is null and j.user_id = pg.user_id and j.etsy_listing_id = pg.etsy_listing_id and j.status in ('queued', 'running')));
$$;
revoke all on function public.library_page(text, text, text, int, int) from public, anon;
grant execute on function public.library_page(text, text, text, int, int) to authenticated;

-- ---------- L3 / L5 ----------
create index if not exists batches_user_idx on public.batches (user_id);

create or replace function public.purge_expired_provider_cache(p_grace interval default interval '1 hour')
returns int language sql security definer set search_path = public as $$
  with d as (delete from public.provider_cache where expires_at < now() - p_grace returning 1) select count(*)::int from d;
$$;
revoke all on function public.purge_expired_provider_cache(interval) from public, anon, authenticated;
grant execute on function public.purge_expired_provider_cache(interval) to service_role;
