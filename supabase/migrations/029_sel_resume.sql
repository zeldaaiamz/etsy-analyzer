-- 029：获取链接「继续本批」（QA 2026-10-16 S-4）。
-- 停止后：已通过的 sel_items 已逐条写入、sel_batches.scraped_count 每 5 条刷新，因此保留不丢。
-- sel_resume 把最近一个已停止的批次重新排队；worker 从 offset_start + scraped_count 继续读（需要同步更新 box 上的 ehunt_worker.py）。
create or replace function public.sel_resume(p_run uuid) returns boolean
language plpgsql security definer set search_path = public as $$
declare r public.sel_runs; b public.sel_batches; uid uuid := (select auth.uid());
begin
  select * into r from public.sel_runs where id = p_run and user_id = uid and status = 'canceled' for update;
  if not found then return false; end if;
  if exists (select 1 from public.sel_runs where user_id = uid and id <> p_run and status in ('count_queued','counting','queued','running')) then raise exception '已有进行中的任务'; end if;
  select * into b from public.sel_batches where run_id = p_run and status = 'canceled' order by batch_no desc limit 1 for update;
  if not found then return false; end if;
  update public.sel_batches set status = 'queued', finished_at = null where id = b.id;
  update public.sel_runs set status = 'queued', finished_at = null, error = null, current_batch = b.batch_no, attempts = 0 where id = p_run;
  return true;
end $$;
revoke all on function public.sel_resume(uuid) from public, anon;
grant execute on function public.sel_resume(uuid) to authenticated;
