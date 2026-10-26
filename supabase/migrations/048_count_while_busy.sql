-- 048：抓取/查询进行中也能查询新关键词（Hongyan 2026-10-20 12:18）。未在生产执行，需批准。
-- 037 已去掉「有任务进行中」限制，但 sel_count 仍保留「已有正在查询数量的任务」→ 别的词在查询数量时不能再查。
-- 改为只防连点：同一用户、同一关键词（大小写/空格不敏感）已在排队/查询数量中 → 直接返回那个任务 id（幂等），不报错；其它词照常排队。
-- worker 认领顺序不变（037：查询数量 > queue_at），一次一个，速度不变。043 关键词记录触发器只在真正插入时记一条（重复点不重复记）。
create or replace function public.sel_count(p_keyword text, p_params jsonb default '{}'::jsonb)
returns uuid language plpgsql security definer set search_path = public as $$
declare rid uuid; uid uuid := (select auth.uid()); kw text := btrim(regexp_replace(coalesce(p_keyword, ''), '\s+', ' ', 'g'));
begin
  if uid is null or kw = '' then raise exception '需要登录并填写关键词'; end if;
  if length(kw) > 200 then raise exception '关键词太长（最多 200 字）'; end if;
  perform pg_advisory_xact_lock(hashtext('sel_count:' || uid::text));
  select id into rid from public.sel_runs where user_id = uid and status in ('count_queued','counting') and lower(keyword) = lower(kw) order by created_at desc limit 1;
  if rid is not null then return rid; end if;
  insert into public.sel_runs (user_id, keyword, status, params)
  values (uid, kw, 'count_queued',
          jsonb_build_object('price_min',8,'price_max',40,'min_sales',100,'min_sales_7d',4,'sort','↓7-day sales') || coalesce(p_params, '{}'::jsonb)
          || jsonb_build_object('mode','convertible','runs',3,'optional_custom','keep'))
  returning id into rid;
  return rid;
end $$;
revoke all on function public.sel_count(text, jsonb) from public, anon;
grant execute on function public.sel_count(text, jsonb) to authenticated;
