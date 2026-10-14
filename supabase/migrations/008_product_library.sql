-- 008：选品备选库（产品库）+ 删除任务 + 批量操作（全部追加式，旧前端不受影响）

-- ---------- 产品库 ----------
-- 保存快照字段：任务被删除后条目仍保留（job_id 置空），前端删除前会提示“已在产品库”
create table if not exists public.product_library (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  job_id uuid references public.jobs(id) on delete set null,
  etsy_listing_id bigint not null,
  etsy_url text,
  title text,
  image_url text,
  price numeric,
  currency text,
  shop_name text,
  product_summary text,
  target_audience text,
  keywords text[],
  identical_count int,
  similar_count int,
  note text,
  added_at timestamptz not null default now(),
  unique (user_id, etsy_listing_id)
);
create index if not exists product_library_user_added_idx on public.product_library (user_id, added_at desc);
create index if not exists product_library_job_idx on public.product_library (job_id);
alter table public.product_library enable row level security;
create policy library_select on public.product_library for select to authenticated using (user_id = (select auth.uid()));
revoke insert, update, delete on public.product_library from anon, authenticated;
revoke all on public.product_library from anon;

-- 加入：仅本人、仅已完成任务；同一 listing 已存在则刷新快照（保留备注与加入时间）
create or replace function public.add_to_library(p_jobs uuid[]) returns int
language plpgsql security definer set search_path = public as $$
declare n int;
begin
  if (select auth.uid()) is null then return 0; end if;
  insert into public.product_library as l (user_id, job_id, etsy_listing_id, etsy_url, title, image_url, price, currency, shop_name,
         product_summary, target_audience, keywords, identical_count, similar_count)
  select distinct on (coalesce(j.etsy_listing_id, e.listing_id))
         j.user_id, j.id, coalesce(j.etsy_listing_id, e.listing_id), j.etsy_url, e.title, e.image_url, e.price, e.currency, e.shop_name,
         a.product_summary, a.target_audience, a.keywords,
         (select count(*) from public.evidence_snapshots s where s.job_id = j.id and s.kind = 'identical'),
         (select count(*) from public.evidence_snapshots s where s.job_id = j.id and s.kind = 'similar')
    from public.jobs j
    left join public.etsy_products e on e.job_id = j.id
    left join public.analyses a on a.job_id = j.id
   where j.id = any(p_jobs) and j.user_id = (select auth.uid()) and j.status = 'succeeded'
     and coalesce(j.etsy_listing_id, e.listing_id) is not null
   order by coalesce(j.etsy_listing_id, e.listing_id), j.created_at desc
  on conflict (user_id, etsy_listing_id) do update set job_id = excluded.job_id, etsy_url = excluded.etsy_url, title = excluded.title,
    image_url = excluded.image_url, price = excluded.price, currency = excluded.currency, shop_name = excluded.shop_name,
    product_summary = excluded.product_summary, target_audience = excluded.target_audience, keywords = excluded.keywords,
    identical_count = excluded.identical_count, similar_count = excluded.similar_count;
  get diagnostics n = row_count;
  return n;
end $$;

-- 移出：按条目 id 或按任务（任务对应的 listing）
create or replace function public.remove_from_library(p_items uuid[] default null, p_jobs uuid[] default null) returns int
language plpgsql security definer set search_path = public as $$
declare n int;
begin
  delete from public.product_library l
   where l.user_id = (select auth.uid())
     and (l.id = any(coalesce(p_items, '{}'))
          or l.job_id = any(coalesce(p_jobs, '{}'))
          or l.etsy_listing_id in (select coalesce(j.etsy_listing_id, e.listing_id) from public.jobs j
                                     left join public.etsy_products e on e.job_id = j.id
                                    where j.id = any(coalesce(p_jobs, '{}')) and j.user_id = (select auth.uid())));
  get diagnostics n = row_count;
  return n;
end $$;

create or replace function public.update_library_note(p_item uuid, p_note text) returns boolean
language sql security definer set search_path = public as $$
  with u as (update public.product_library set note = nullif(left(p_note, 2000), '')
              where id = p_item and user_id = (select auth.uid()) returning 1)
  select exists(select 1 from u);
$$;

-- ---------- 删除任务（级联删除 etsy_products/analyses/amazon_matches/evidence_snapshots）----------
-- 运行中的任务不可删（需先停止）；返回 {deleted, skipped_running, paths}，paths 供前端清理 Storage
create or replace function public.delete_jobs(p_jobs uuid[]) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_paths text[]; v_del int; v_run int;
begin
  if (select auth.uid()) is null then return jsonb_build_object('deleted', 0, 'skipped_running', 0, 'paths', '[]'::jsonb); end if;
  select count(*) into v_run from public.jobs where id = any(p_jobs) and user_id = (select auth.uid()) and status = 'running';
  select coalesce(array_agg(s.image_path), '{}') into v_paths
    from public.evidence_snapshots s join public.jobs j on j.id = s.job_id
   where j.id = any(p_jobs) and j.user_id = (select auth.uid()) and j.status <> 'running' and s.image_path is not null;
  delete from public.jobs where id = any(p_jobs) and user_id = (select auth.uid()) and status <> 'running';
  get diagnostics v_del = row_count;
  return jsonb_build_object('deleted', v_del, 'skipped_running', v_run, 'paths', to_jsonb(v_paths));
end $$;

-- 批量重新提交（失败/取消/已完成）
create or replace function public.retry_jobs(p_jobs uuid[]) returns int
language plpgsql security definer set search_path = public as $$
declare n int;
begin
  update public.jobs set status='queued', attempts=0, error=null, stage=null, queued_at=now(), run_after=now(),
         started_at=null, finished_at=null, reviewed_at=null, locked_by=null, locked_at=null, cancel_requested=false
   where id = any(p_jobs) and user_id = (select auth.uid()) and status in ('failed','canceled');
  get diagnostics n = row_count;
  return n;
end $$;

revoke all on function public.add_to_library(uuid[]) from public, anon;
revoke all on function public.remove_from_library(uuid[], uuid[]) from public, anon;
revoke all on function public.update_library_note(uuid, text) from public, anon;
revoke all on function public.delete_jobs(uuid[]) from public, anon;
revoke all on function public.retry_jobs(uuid[]) from public, anon;
grant execute on function public.add_to_library(uuid[]) to authenticated;
grant execute on function public.remove_from_library(uuid[], uuid[]) to authenticated;
grant execute on function public.update_library_note(uuid, text) to authenticated;
grant execute on function public.delete_jobs(uuid[]) to authenticated;
grant execute on function public.retry_jobs(uuid[]) to authenticated;

-- 允许用户删除自己目录下的快照文件（删除任务后由前端清理）
drop policy if exists snapshots_owner_delete on storage.objects;
create policy snapshots_owner_delete on storage.objects for delete to authenticated
  using (bucket_id = 'snapshots' and (storage.foldername(name))[1] = (select auth.uid())::text);
