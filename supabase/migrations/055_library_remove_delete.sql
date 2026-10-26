-- 055：产品库「移出产品库」改为直接删除（不可恢复）
-- 旧 remove_from_library：只删 product_library 行 → 任务因 task_jobs 过滤解除而回到「我的任务」
-- 新 delete_library_items：删产品库条目 + 本人对应 listing 的非运行中任务（分析/匹配/截图 on delete cascade）
-- 不删 listing_scrape_status / listing_reviews（按 listing_id 关联、无 FK，可能被其它用户或再加入复用；与 022 一致）
-- remove_from_library 保留给「加入后撤销」：仍只移出库、任务回列表

create or replace function public.delete_library_items(p_items uuid[] default null, p_jobs uuid[] default null)
returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  u uuid := (select auth.uid());
  v_lib int := 0;
  v_del int := 0;
  v_run int := 0;
  v_paths text[];
  v_lids bigint[];
  v_job_ids uuid[];
begin
  if u is null then
    return jsonb_build_object('deleted_library', 0, 'deleted_jobs', 0, 'skipped_running', 0, 'paths', '[]'::jsonb);
  end if;

  -- 匹配本人库条目（按条目 id 或按任务 / listing）
  select coalesce(array_agg(distinct l.etsy_listing_id), '{}'),
         coalesce(array_agg(distinct l.job_id) filter (where l.job_id is not null), '{}')
    into v_lids, v_job_ids
    from public.product_library l
   where l.user_id = u
     and (l.id = any(coalesce(p_items, '{}'))
          or l.job_id = any(coalesce(p_jobs, '{}'))
          or l.etsy_listing_id in (
               select coalesce(j.etsy_listing_id, e.listing_id)
                 from public.jobs j
                 left join public.etsy_products e on e.job_id = j.id
                where j.id = any(coalesce(p_jobs, '{}')) and j.user_id = u));

  if coalesce(array_length(v_lids, 1), 0) = 0 and coalesce(array_length(v_job_ids, 1), 0) = 0 then
    return jsonb_build_object('deleted_library', 0, 'deleted_jobs', 0, 'skipped_running', 0, 'paths', '[]'::jsonb);
  end if;

  delete from public.product_library l
   where l.user_id = u
     and (l.etsy_listing_id = any(v_lids) or l.id = any(coalesce(p_items, '{}')));
  get diagnostics v_lib = row_count;

  -- 运行中的不删（库条目一般来自 succeeded，极少见）
  select count(*) into v_run
    from public.jobs j
   where j.user_id = u and j.status = 'running'
     and (j.id = any(v_job_ids) or j.etsy_listing_id = any(v_lids)
          or exists (select 1 from public.etsy_products e where e.job_id = j.id and e.listing_id = any(v_lids)));

  select coalesce(array_agg(s.image_path), '{}') into v_paths
    from public.evidence_snapshots s
    join public.jobs j on j.id = s.job_id
   where j.user_id = u and j.status <> 'running' and s.image_path is not null
     and (j.id = any(v_job_ids) or j.etsy_listing_id = any(v_lids)
          or exists (select 1 from public.etsy_products e where e.job_id = j.id and e.listing_id = any(v_lids)));

  delete from public.jobs j
   where j.user_id = u and j.status <> 'running'
     and (j.id = any(v_job_ids) or j.etsy_listing_id = any(v_lids)
          or exists (select 1 from public.etsy_products e where e.job_id = j.id and e.listing_id = any(v_lids)));
  get diagnostics v_del = row_count;

  return jsonb_build_object(
    'deleted_library', v_lib,
    'deleted_jobs', v_del,
    'skipped_running', v_run,
    'paths', to_jsonb(coalesce(v_paths, '{}'))
  );
end $$;

revoke all on function public.delete_library_items(uuid[], uuid[]) from public, anon;
grant execute on function public.delete_library_items(uuid[], uuid[]) to authenticated;

-- 一键删除全部「不考虑」：与卡片/批量同语义（直接删除，不回任务列表）
-- 返回类型 int → jsonb，须先 drop
drop function if exists public.delete_rejected_library();
create function public.delete_rejected_library() returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  u uuid := (select auth.uid());
  ids uuid[];
begin
  if u is null then
    return jsonb_build_object('deleted_library', 0, 'deleted_jobs', 0, 'skipped_running', 0, 'paths', '[]'::jsonb);
  end if;
  select coalesce(array_agg(id), '{}') into ids
    from public.product_library where user_id = u and stage = 'rejected';
  if coalesce(array_length(ids, 1), 0) = 0 then
    return jsonb_build_object('deleted_library', 0, 'deleted_jobs', 0, 'skipped_running', 0, 'paths', '[]'::jsonb);
  end if;
  return public.delete_library_items(ids, null);
end $$;

revoke all on function public.delete_rejected_library() from public, anon;
grant execute on function public.delete_rejected_library() to authenticated;
