-- 058：选品雷达「从 ABA 导入 10 个」选择器（预览，待 Hongyan 批准）
-- 057 是“领取即导入”：一次 aba_claim_batch 领 5 个全部建任务。现在改为两步：
--   ① aba_preview_for_selection(p_limit=10)：只读，列出尚未发给 'selection-radar' 的前 N 个词根（与 aba_claim_batch 同一排序），
--      附 root_zh（aba_root_words 已有字段，空则前端显示“暂无中文”）和 seen_count（sel_keyword_log 里查过几次，只读，永不删除）。
--      不写 aba_root_sends —— 没选中的词不消耗，下次仍会出现。
--   ② aba_import_selected(p_ids)：按数组顺序，仅对“尚未发给 selection-radar”的词根写 aba_root_sends 并调用 sel_count(原逻辑)，
--      与手动「查询数量」同一条路（写 sel_keyword_log、048 进行中可排队、052 查到数量后自动入队抓取）。已导入过的跳过（不重复获取）。
-- 不改表结构；不碰 sel_keyword_log 删除；057 函数保留不动（向后兼容）。
create or replace function public.aba_preview_for_selection(p_limit integer default 10)
returns table(root_id bigint, keyword text, root_zh text, best_rank integer, term_count integer, seen_count integer)
language plpgsql stable security definer set search_path to 'public'
as $$
begin
  if (select auth.uid()) is null then raise exception '需要登录'; end if;
  return query
  select r.id, r.root_word, nullif(btrim(r.root_zh), ''), r.best_rank, r.term_count,
    (select count(*)::int from public.sel_keyword_log l where lower(btrim(l.keyword)) = lower(btrim(r.root_word)))
  from public.aba_root_words r
  where not exists (select 1 from public.aba_root_sends s where s.root_id = r.id and s.consumer = 'selection-radar')
  order by r.best_rank nulls last, r.term_count desc, r.id
  limit least(greatest(coalesce(p_limit, 10), 1), 20);
end $$;

create or replace function public.aba_import_selected(p_ids bigint[])
returns table(root_id bigint, keyword text, run_id uuid)
language plpgsql security definer set search_path to 'public'
as $$
declare r record; c text;
  f jsonb := jsonb_build_object('price_min',8,'price_max',40,'min_sales',100,'min_sales_7d',4,'sort','↓7-day sales');
begin
  if (select auth.uid()) is null then raise exception '需要登录'; end if;
  if coalesce(cardinality(p_ids), 0) = 0 then raise exception '请至少选择 1 个词'; end if;
  if cardinality(p_ids) > 20 then raise exception '一次最多导入 20 个'; end if;
  c := public.aba_check_consumer('selection-radar');
  perform pg_advisory_xact_lock(hashtext('aba_claim_batch:' || c)); -- 与 aba_claim_batch 同一把锁，防并发重复领取
  for r in
    select w.id, w.root_word, min(u.ord) ord
    from unnest(p_ids) with ordinality u(id, ord) join public.aba_root_words w on w.id = u.id
    where not exists (select 1 from public.aba_root_sends s where s.root_id = w.id and s.consumer = c)
    group by w.id, w.root_word order by min(u.ord)
  loop
    insert into public.aba_root_sends(root_id, consumer) values (r.id, c) on conflict do nothing;
    root_id := r.id; keyword := r.root_word;
    run_id := public.sel_count(r.root_word, f);
    return next;
  end loop;
end $$;
revoke all on function public.aba_preview_for_selection(integer) from public, anon;
revoke all on function public.aba_import_selected(bigint[]) from public, anon;
grant execute on function public.aba_preview_for_selection(integer) to authenticated, service_role;
grant execute on function public.aba_import_selected(bigint[]) to authenticated, service_role;
