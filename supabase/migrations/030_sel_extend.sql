-- 030：获取链接「继续读取」（2026-10-16 Hongyan）。
-- 已完成或本批完成的 run，如果读过的条数 < min(结果数, 5000)，可以从下一条接着读，不重读。
-- sel_extend(p_run, p_add, p_all)：从已有批次的最大 offset_end + 1 开始，max_rows 增加 p_add（不超过 total_count 和 5000），
-- 先排一批（≤1000 条）；p_all = true 时 worker 读完一批自动排下一批，直到 max_rows。
-- 配套 worker 改动：结束条件只看 remaining（不再是 batch_no >= 5）。
create or replace function public.sel_extend(p_run uuid, p_add int, p_all boolean default false)
returns int language plpgsql security definer set search_path = public as $$
declare r public.sel_runs; uid uuid := (select auth.uid()); s int; e int; newmax int; nb int;
begin
  select * into r from public.sel_runs where id = p_run and user_id = uid and status in ('batch_done','succeeded') for update;
  if not found then return 0; end if;
  if exists (select 1 from public.sel_runs where user_id = uid and id <> p_run and status in ('count_queued','counting','queued','running')) then raise exception '已有进行中的任务'; end if;
  select coalesce(max(offset_end), 0) + 1, coalesce(max(batch_no), 0) + 1 into s, nb from public.sel_batches where run_id = p_run;
  newmax := least(s - 1 + greatest(coalesce(p_add, 0), 0), coalesce(r.total_count, 0), 5000);
  if newmax < s then return 0; end if;
  e := least(s + coalesce(r.batch_size, 1000) - 1, newmax);
  insert into public.sel_batches (run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end)
  values (p_run, r.user_id, nb, s, e, (s + 19) / 20, (e + 19) / 20);
  update public.sel_runs set status = 'queued', max_rows = newmax, current_batch = nb, read_all = coalesce(p_all, false),
         finished_at = null, error = null, attempts = 0,
         params = params || jsonb_build_object('limit', newmax)
   where id = p_run;
  return nb;
end $$;
revoke all on function public.sel_extend(uuid, int, boolean) from public, anon;
grant execute on function public.sel_extend(uuid, int, boolean) to authenticated;
