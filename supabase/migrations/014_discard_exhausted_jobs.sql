-- 014：任务第 3 次（max_attempts）尝试仍失败 → 自动删除任务（级联删除 etsy_products/analyses/amazon_matches/evidence_snapshots）
-- 例外：该 listing 已在本人产品库（按 job_id 或 listing）→ 标记 failed 保留（与 013 delete_jobs 规则一致）
-- 纯追加：新函数仅 service_role 可调用；并一次性清理已排队但 attempts >= max_attempts 的任务（额度暂停的不计入，因为其 attempts 已回退）
create or replace function public.discard_exhausted_job(p_job uuid, p_error text) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_paths text[]; v_lib boolean;
begin
  select exists (select 1 from public.jobs j join public.product_library l on l.user_id = j.user_id
                  and (l.job_id = j.id or l.etsy_listing_id = coalesce(j.etsy_listing_id,
                       (select e.listing_id from public.etsy_products e where e.job_id = j.id limit 1)))
                 where j.id = p_job) into v_lib;
  if v_lib then
    update public.jobs set status = 'failed', error = left(p_error, 1000), finished_at = now(), locked_by = null where id = p_job;
    return jsonb_build_object('action', 'failed', 'paths', '[]'::jsonb);
  end if;
  select coalesce(array_agg(s.image_path), '{}') into v_paths
    from public.evidence_snapshots s where s.job_id = p_job and s.image_path is not null;
  delete from public.jobs where id = p_job;
  return jsonb_build_object('action', 'deleted', 'paths', to_jsonb(v_paths));
end $$;
revoke all on function public.discard_exhausted_job(uuid, text) from public, anon, authenticated;
grant execute on function public.discard_exhausted_job(uuid, text) to service_role;

-- 一次性清理：已排队且尝试次数用尽的任务
select public.discard_exhausted_job(j.id, coalesce(j.error, '多次尝试失败'))
  from public.jobs j where j.status = 'queued' and j.attempts >= j.max_attempts;
