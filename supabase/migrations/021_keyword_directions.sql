-- 021：PM-001 关键词「五个方向」（direction 1-5）。纯追加：只替换 update_library_keywords（签名不变），保留/接受 direction。
-- direction：1 精准词 2 同场景相似品 3 产品文案/卖点词 4 人群礼物词 5 细分场景词；人工新增的词可选方向（缺省为空）
create or replace function public.update_library_keywords(p_item uuid, p_keywords jsonb) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_old jsonb; v_new jsonb;
begin
  if (select auth.uid()) is null then raise exception 'not signed in' using errcode = '42501'; end if;
  select amazon_keywords into v_old from public.product_library where id = p_item and user_id = (select auth.uid());
  if not found then raise exception 'not found' using errcode = 'P0002'; end if;
  if jsonb_typeof(p_keywords) is distinct from 'array' then raise exception 'keywords must be an array' using errcode = '22023'; end if;
  select coalesce(jsonb_agg(jsonb_strip_nulls(jsonb_build_object('keyword', kw,
           'source', coalesce(o->>'source', 'manual'),
           'basis', coalesce(o->>'basis', '人工添加'),
           'direction', coalesce(case when dir between 1 and 5 then dir end, (o->>'direction')::int)))
           order by ord), '[]'::jsonb)
    into v_new
    from (select distinct on (kw) kw, ord, dir from (
            select lower(regexp_replace(btrim(coalesce(e->>'keyword', e #>> '{}')), '\s+', ' ', 'g')) kw, ord,
                   case when jsonb_typeof(e) = 'object' and (e->>'direction') ~ '^[1-5]$' then (e->>'direction')::int end dir
              from jsonb_array_elements(p_keywords) with ordinality t(e, ord)) x
           where kw <> '' order by kw, ord) d
    left join lateral (select o from jsonb_array_elements(v_old) o where lower(btrim(o->>'keyword')) = d.kw limit 1) old on true;
  if jsonb_array_length(v_new) > 5 then raise exception '最多 5 个关键词' using errcode = '22023'; end if;
  if not public.valid_amazon_keywords(v_new) then raise exception 'invalid keywords' using errcode = '22023'; end if;
  update public.product_library set amazon_keywords = v_new, keywords_edited_at = now()
   where id = p_item and user_id = (select auth.uid());
  return v_new;
end $$;
revoke all on function public.update_library_keywords(uuid, jsonb) from public, anon;
grant execute on function public.update_library_keywords(uuid, jsonb) to authenticated;
