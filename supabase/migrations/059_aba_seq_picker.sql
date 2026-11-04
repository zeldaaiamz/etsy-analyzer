-- 059：「从 ABA 导入 10 个」改为依次出词（Hongyan 2026-11-04：没选中的词下次不要出现；每点一次出 10 个不一样的词，按词语表依次抓）
-- 设计：
--   * 新表 sel_aba_shown(user_id, root_id)：每个用户“已经看过”的词根 = 服务端游标（换浏览器/清缓存也不会回头）。只增不删。
--   * aba_next_for_selection(p_limit=10)：按 ABA 原排序（best_rank, term_count desc, id）取下一批，并在返回前写入 sel_aba_shown
--     —— 打开即消耗：确认、取消、关掉对话框都算“出现过”，没选的词下次不再出现。
--   * 跳过：已发给 selection-radar 的（已导入）、本用户看过的、sel_keyword_log 里已查过的（大小写/空格不敏感）。
--   * 到底了返回空，前端提示“ABA 词根已全部出过”。
--   * 导入仍用 058 的 aba_import_selected（不改）；058 的 aba_preview_for_selection 保留（向后兼容），前端不再调用。
--   * 不删任何数据，不碰 sel_keyword_log。
create table if not exists public.sel_aba_shown(
  user_id uuid not null,
  root_id bigint not null,
  shown_at timestamptz not null default now(),
  primary key (user_id, root_id)
);
alter table public.sel_aba_shown enable row level security;
do $$ begin
  create policy sel_aba_shown_own_read on public.sel_aba_shown for select to authenticated using (user_id = (select auth.uid()));
exception when duplicate_object then null; end $$;
revoke all on public.sel_aba_shown from public, anon;
grant select on public.sel_aba_shown to authenticated;

create or replace function public.aba_next_for_selection(p_limit integer default 10)
returns table(root_id bigint, keyword text, root_zh text, best_rank integer, term_count integer)
language plpgsql volatile security definer set search_path to 'public'
as $$
declare u uuid := (select auth.uid());
begin
  if u is null then raise exception '需要登录'; end if;
  perform pg_advisory_xact_lock(hashtext('aba_next_for_selection:' || u::text)); -- 同一用户连点两次不会拿到同一批
  return query
  with nxt as (
    select r.id, r.root_word, nullif(btrim(r.root_zh), '') zh, r.best_rank br, r.term_count tc
    from public.aba_root_words r
    where not exists (select 1 from public.aba_root_sends s where s.root_id = r.id and s.consumer = 'selection-radar')
      and not exists (select 1 from public.sel_aba_shown h where h.user_id = u and h.root_id = r.id)
      and not exists (select 1 from public.sel_keyword_log l where lower(btrim(l.keyword)) = lower(btrim(r.root_word)))
    order by r.best_rank nulls last, r.term_count desc, r.id
    limit least(greatest(coalesce(p_limit, 10), 1), 20)
  ), ins as (
    insert into public.sel_aba_shown(user_id, root_id) select u, n.id from nxt n on conflict do nothing
  )
  select n.id, n.root_word, n.zh, n.br, n.tc from nxt n order by n.br nulls last, n.tc desc, n.id;
end $$;
revoke all on function public.aba_next_for_selection(integer) from public, anon;
grant execute on function public.aba_next_for_selection(integer) to authenticated, service_role;
