-- 009：产品库阶段（备选 candidate / 进行中 active / 不考虑 rejected）+ 不考虑原因（全部追加式，旧前端不受影响）
-- 新列有默认值，add_to_library 的 on conflict 不更新这些列 → 重复加入不会重置阶段
alter table public.product_library
  add column if not exists stage text not null default 'candidate',
  add column if not exists reject_reasons text[] not null default '{}',
  add column if not exists stage_note text,
  add column if not exists stage_changed_at timestamptz;
do $$ begin
  alter table public.product_library add constraint product_library_stage_chk check (stage in ('candidate','active','rejected'));
exception when duplicate_object then null; end $$;
do $$ begin
  alter table public.product_library add constraint product_library_reasons_chk check (
    reject_reasons <@ array['roi_low','cost_high','ip_risk','competition','demand_low','seasonal','logistics','compliance','supply','other']::text[]
    and (stage = 'rejected') = (cardinality(reject_reasons) > 0)
    and ('other' <> all(reject_reasons) or coalesce(btrim(stage_note), '') <> ''));
exception when duplicate_object then null; end $$;
create index if not exists product_library_user_stage_idx on public.product_library (user_id, stage);

-- 阶段变更：仅 candidate→active / candidate→rejected，以及 active|rejected→candidate（撤销）
-- 返回实际更新条数；非法转换的条目被跳过（不报错），参数非法时报错
create or replace function public.set_library_stage(p_items uuid[], p_stage text, p_reasons text[] default null, p_note text default null)
returns int language plpgsql security definer set search_path = public as $$
declare n int; v_reasons text[] := coalesce((select array_agg(distinct r) from unnest(coalesce(p_reasons, '{}')) r), '{}');
  v_note text := nullif(btrim(left(coalesce(p_note, ''), 2000)), '');
begin
  if (select auth.uid()) is null then return 0; end if;
  if p_stage not in ('candidate','active','rejected') then raise exception 'invalid stage: %', p_stage using errcode = '22023'; end if;
  if p_stage = 'rejected' then
    if cardinality(v_reasons) = 0 then raise exception '请至少选择一个不考虑原因' using errcode = '22023'; end if;
    if not v_reasons <@ array['roi_low','cost_high','ip_risk','competition','demand_low','seasonal','logistics','compliance','supply','other']::text[]
      then raise exception 'invalid reason' using errcode = '22023'; end if;
    if 'other' = any(v_reasons) and v_note is null then raise exception '选择“其他”时必须填写说明' using errcode = '22023'; end if;
  else v_reasons := '{}';
  end if;
  update public.product_library set stage = p_stage, reject_reasons = v_reasons,
         stage_note = case when p_stage = 'rejected' then v_note else null end, stage_changed_at = now()
   where id = any(coalesce(p_items, '{}')) and user_id = (select auth.uid())
     and ((p_stage in ('active','rejected') and stage = 'candidate') or (p_stage = 'candidate' and stage in ('active','rejected')));
  get diagnostics n = row_count;
  return n;
end $$;
revoke all on function public.set_library_stage(uuid[], text, text[], text) from public, anon;
grant execute on function public.set_library_stage(uuid[], text, text[], text) to authenticated;
