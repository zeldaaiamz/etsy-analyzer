-- 020：PM-001 一键亚马逊搜索（纯追加：新列有默认值，函数签名不变，library_page 只多返回两个键 → 线上旧前端不受影响）
-- amazon_keywords：[{keyword, source, basis}]，source ∈ title/image/review/original-title/manual，≤5 个
alter table public.analyses add column if not exists keyword_items jsonb not null default '[]'::jsonb;
alter table public.product_library
  add column if not exists amazon_keywords jsonb not null default '[]'::jsonb,
  add column if not exists keywords_edited_at timestamptz;

create or replace function public.valid_amazon_keywords(k jsonb) returns boolean
language sql immutable set search_path = public as $$
  select jsonb_typeof(k) = 'array' and jsonb_array_length(k) <= 5
     and not exists (select 1 from jsonb_array_elements(k) e
                      where jsonb_typeof(e) <> 'object' or coalesce(btrim(e->>'keyword'), '') = '' or length(e->>'keyword') > 80
                         or coalesce(e->>'source', '') not in ('title','image','review','original-title','manual'))
     and (select count(distinct lower(btrim(e->>'keyword'))) from jsonb_array_elements(k) e) = jsonb_array_length(k);
$$;
do $$ begin
  alter table public.product_library add constraint product_library_amazon_keywords_chk check (public.valid_amazon_keywords(amazon_keywords));
exception when duplicate_object then null; end $$;

-- 人工编辑：整体替换。已有词保留原来源；新增词一律记为 manual（前端也这么做，这里再强制一次）
create or replace function public.update_library_keywords(p_item uuid, p_keywords jsonb) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_old jsonb; v_new jsonb;
begin
  if (select auth.uid()) is null then raise exception 'not signed in' using errcode = '42501'; end if;
  select amazon_keywords into v_old from public.product_library where id = p_item and user_id = (select auth.uid());
  if not found then raise exception 'not found' using errcode = 'P0002'; end if;
  if jsonb_typeof(p_keywords) is distinct from 'array' then raise exception 'keywords must be an array' using errcode = '22023'; end if;
  select coalesce(jsonb_agg(jsonb_build_object('keyword', kw,
           'source', coalesce((select o->>'source' from jsonb_array_elements(v_old) o where lower(btrim(o->>'keyword')) = kw limit 1), 'manual'),
           'basis', coalesce((select o->>'basis' from jsonb_array_elements(v_old) o where lower(btrim(o->>'keyword')) = kw limit 1), '人工添加'))
           order by ord), '[]'::jsonb)
    into v_new
    from (select distinct on (kw) kw, ord from (
            select lower(regexp_replace(btrim(coalesce(e->>'keyword', e #>> '{}')), '\s+', ' ', 'g')) kw, ord
              from jsonb_array_elements(p_keywords) with ordinality t(e, ord)) x
           where kw <> '' order by kw, ord) d;
  if jsonb_array_length(v_new) > 5 then raise exception '最多 5 个关键词' using errcode = '22023'; end if;
  if not public.valid_amazon_keywords(v_new) then raise exception 'invalid keywords' using errcode = '22023'; end if;
  update public.product_library set amazon_keywords = v_new, keywords_edited_at = now()
   where id = p_item and user_id = (select auth.uid());
  return v_new;
end $$;
revoke all on function public.update_library_keywords(uuid, jsonb) from public, anon;
grant execute on function public.update_library_keywords(uuid, jsonb) to authenticated;

create or replace function public.add_to_library(p_jobs uuid[]) returns int
language plpgsql security definer set search_path = public as $$
declare n int;
begin
  if (select auth.uid()) is null then return 0; end if;
  insert into public.product_library as l (user_id, job_id, etsy_listing_id, etsy_url, title, image_url, price, currency, shop_name,
         product_summary, target_audience, keywords, identical_count, similar_count, amazon_keywords)
  select distinct on (coalesce(j.etsy_listing_id, e.listing_id))
         j.user_id, j.id, coalesce(j.etsy_listing_id, e.listing_id), j.etsy_url, e.title, e.image_url, e.price, e.currency, e.shop_name,
         a.product_summary, a.target_audience, a.keywords,
         (select count(*) from public.evidence_snapshots s where s.job_id = j.id and s.kind = 'identical'),
         (select count(*) from public.evidence_snapshots s where s.job_id = j.id and s.kind = 'similar'),
         coalesce(a.keyword_items, '[]'::jsonb)
    from public.jobs j
    left join public.etsy_products e on e.job_id = j.id
    left join public.analyses a on a.job_id = j.id
   where j.id = any(p_jobs) and j.user_id = (select auth.uid()) and j.status = 'succeeded'
     and coalesce(j.etsy_listing_id, e.listing_id) is not null
   order by coalesce(j.etsy_listing_id, e.listing_id), j.created_at desc
  on conflict (user_id, etsy_listing_id) do update set job_id = excluded.job_id, etsy_url = excluded.etsy_url, title = excluded.title,
    image_url = excluded.image_url, price = excluded.price, currency = excluded.currency, shop_name = excluded.shop_name,
    product_summary = excluded.product_summary, target_audience = excluded.target_audience, keywords = excluded.keywords,
    identical_count = excluded.identical_count, similar_count = excluded.similar_count,
    -- 人工改过（含 manual 来源或 keywords_edited_at）就保留人工版本；新分析没有关键词时也不覆盖
    amazon_keywords = case when l.keywords_edited_at is not null or jsonb_array_length(excluded.amazon_keywords) = 0
                           then l.amazon_keywords else excluded.amazon_keywords end;
  get diagnostics n = row_count;
  return n;
end $$;

revoke all on function public.add_to_library(uuid[]) from public, anon;
grant execute on function public.add_to_library(uuid[]) to authenticated;

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
               'added_at', added_at, 'stage', stage, 'reject_reasons', reject_reasons, 'stage_note', stage_note, 'stage_changed_at', stage_changed_at,
               'amazon_keywords', amazon_keywords, 'competitor_asin', (select m.asin from public.amazon_matches m where m.job_id = pg.job_id
                 and m.match_type = 'identical' and m.asin ~ '^[A-Z0-9]{10}$' order by m.similarity desc nulls last, m.created_at limit 1))
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
