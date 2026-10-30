-- 057：从 ABA 词根库导入到选品雷达（Hongyan 批准 2026-10-30）
-- aba_import_to_selection(p_limit)：登录用户调用；以 'selection-radar' 身份 aba_claim_batch 领取词根，
-- 每个词根按 sel_count(p_keyword) 原逻辑建“查询数量”任务（同样写 sel_keyword_log、同样的 052 自动入队行为，不额外自动开始）。
create or replace function public.aba_import_to_selection(p_limit integer default 5)
returns table(root_id bigint, keyword text, run_id uuid)
language plpgsql security definer set search_path to 'public'
as $$
declare r record;
  f jsonb := jsonb_build_object('price_min',8,'price_max',40,'min_sales',100,'min_sales_7d',4,'sort','↓7-day sales');
begin
  if (select auth.uid()) is null then raise exception '需要登录'; end if;
  for r in select * from public.aba_claim_batch('selection-radar', least(greatest(coalesce(p_limit,5),1),20)) loop
    root_id := r.id; keyword := r.root_word;
    run_id := public.sel_count(r.root_word, f);
    return next;
  end loop;
end $$;
revoke all on function public.aba_import_to_selection(integer) from public, anon;
grant execute on function public.aba_import_to_selection(integer) to authenticated, service_role;
