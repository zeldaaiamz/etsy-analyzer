-- 035：获取链接 QA 2026-10-19（未在生产执行；依赖 024/029/030；与 034_sel_progress 互不影响，可任意先后）。
-- ① sel_batches.batch_no ≤ 5 约束与 030 sel_extend 冲突：先读 200 条再一键读取全部 4,800 → 第 6 批插入失败。EHunt 上限仍是 5,000 条（由 max_rows 保证），批次号不再限制。
alter table public.sel_batches drop constraint if exists sel_batches_batch_no_check;
alter table public.sel_batches add constraint sel_batches_batch_no_check check (batch_no >= 1);

-- ② sel_count：连点/两个标签页同时点“查询数量”会并发通过“已有进行中的任务”检查 → 建出 2 个任务。按用户加事务级 advisory lock 串行化。
--    关键词：连续空白合成一个空格、最长 200 字（与网页一致）。
create or replace function public.sel_count(p_keyword text, p_params jsonb default '{}'::jsonb)
returns uuid language plpgsql security definer set search_path = public as $$
declare rid uuid; uid uuid := (select auth.uid()); kw text := btrim(regexp_replace(coalesce(p_keyword, ''), '\s+', ' ', 'g'));
begin
  if uid is null or kw = '' then raise exception '需要登录并填写关键词'; end if;
  if length(kw) > 200 then raise exception '关键词太长（最多 200 字）'; end if;
  perform pg_advisory_xact_lock(hashtext('sel_count:' || uid::text));
  if exists (select 1 from public.sel_runs where user_id = uid and status in ('count_queued','counting','queued','running')) then raise exception '已有进行中的任务'; end if;
  insert into public.sel_runs (user_id, keyword, status, params)
  values (uid, kw, 'count_queued',
          jsonb_build_object('price_min',8,'price_max',40,'min_sales',100,'min_sales_7d',4,'sort','↓7-day sales') || coalesce(p_params, '{}'::jsonb)
          || jsonb_build_object('mode','convertible','runs',3,'optional_custom','keep'))
  returning id into rid;
  return rid;
end $$;
revoke all on function public.sel_count(text, jsonb) from public, anon;
grant execute on function public.sel_count(text, jsonb) to authenticated;

-- ③ sel_cancel：
--   a. 排队中（box 暂停）就停止 → 批次原来一直是 queued：页面显示“读取中…”、没有「继续本批」、3 天清理也删不掉（finished_at 为空）。现在一并标记为已停止。
--   b. 只停进行中的任务：确认框打开期间本批刚好读完（batch_done）再点确认 → 原来变成“已停止”但没有可继续的批次，也不能再继续读取（死路）。
create or replace function public.sel_cancel(p_run uuid) returns boolean
language plpgsql security definer set search_path = public as $$
begin
  update public.sel_runs set status = 'canceled', finished_at = now()
   where id = p_run and user_id = (select auth.uid()) and status in ('count_queued','counting','counted','queued','running');
  if not found then return false; end if;
  update public.sel_batches set status = 'canceled', finished_at = now() where run_id = p_run and status in ('queued','running');
  return true;
end $$;
revoke all on function public.sel_cancel(uuid) from public, anon;
grant execute on function public.sel_cancel(uuid) to authenticated;

-- ④ sel_resume：91d4b00 之前失败的批次（run=failed、batch=failed，3 天内仍可见）也能「重试本批」，从断点接着读。
create or replace function public.sel_resume(p_run uuid) returns boolean
language plpgsql security definer set search_path = public as $$
declare r public.sel_runs; b public.sel_batches; uid uuid := (select auth.uid());
begin
  select * into r from public.sel_runs where id = p_run and user_id = uid and status in ('canceled','failed') for update;
  if not found then return false; end if;
  if exists (select 1 from public.sel_runs where user_id = uid and id <> p_run and status in ('count_queued','counting','queued','running')) then raise exception '已有进行中的任务'; end if;
  select * into b from public.sel_batches where run_id = p_run and status in ('canceled','failed') order by batch_no desc limit 1 for update;
  if not found then return false; end if;
  update public.sel_batches set status = 'queued', finished_at = null where id = b.id;
  update public.sel_runs set status = 'queued', finished_at = null, error = null, current_batch = b.batch_no, attempts = 0 where id = p_run;
  return true;
end $$;
revoke all on function public.sel_resume(uuid) from public, anon;
grant execute on function public.sel_resume(uuid) to authenticated;
