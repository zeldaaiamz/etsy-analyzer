-- 013：产品库条目的分析结果不随“删除任务”丢失（纯追加/兼容：函数签名不变，返回值只多一个键）
-- 1) delete_jobs：已在本人产品库（按 job_id 或 listing）的任务不删除，计入 kept_library，分析/匹配/截图保留，产品库“查看分析”仍可用
-- 2) 产品库“重新分析”：同一 listing 的新任务完成后，若产品库条目没有关联任务（任务早已删除），自动关联到新任务
create or replace function public.delete_jobs(p_jobs uuid[]) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_paths text[]; v_del int; v_run int; v_keep int; v_ids uuid[];
begin
  if (select auth.uid()) is null then return jsonb_build_object('deleted', 0, 'skipped_running', 0, 'kept_library', 0, 'paths', '[]'::jsonb); end if;
  select count(*) into v_run from public.jobs where id = any(p_jobs) and user_id = (select auth.uid()) and status = 'running';
  select coalesce(array_agg(j.id), '{}') into v_ids from public.jobs j
   where j.id = any(p_jobs) and j.user_id = (select auth.uid()) and j.status <> 'running'
     and not exists (select 1 from public.product_library l where l.user_id = j.user_id
                      and (l.job_id = j.id or l.etsy_listing_id = coalesce(j.etsy_listing_id,
                           (select e.listing_id from public.etsy_products e where e.job_id = j.id limit 1))));
  select count(*) into v_keep from public.jobs j
   where j.id = any(p_jobs) and j.user_id = (select auth.uid()) and j.status <> 'running' and j.id <> all(v_ids);
  select coalesce(array_agg(s.image_path), '{}') into v_paths
    from public.evidence_snapshots s where s.job_id = any(v_ids) and s.image_path is not null;
  delete from public.jobs where id = any(v_ids);
  get diagnostics v_del = row_count;
  return jsonb_build_object('deleted', v_del, 'skipped_running', v_run, 'kept_library', v_keep, 'paths', to_jsonb(v_paths));
end $$;
revoke all on function public.delete_jobs(uuid[]) from public, anon;
grant execute on function public.delete_jobs(uuid[]) to authenticated;

create or replace function public.link_library_on_success() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.status = 'succeeded' and old.status is distinct from 'succeeded' then
    update public.product_library l set job_id = new.id
     where l.user_id = new.user_id and l.job_id is null
       and l.etsy_listing_id = coalesce(new.etsy_listing_id, (select e.listing_id from public.etsy_products e where e.job_id = new.id limit 1));
  end if;
  return new;
end $$;
revoke all on function public.link_library_on_success() from public, anon, authenticated;
drop trigger if exists jobs_link_library on public.jobs;
create trigger jobs_link_library after update of status on public.jobs for each row execute function public.link_library_on_success();
