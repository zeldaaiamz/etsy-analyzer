-- 052：查询数量成功后自动入队抓取（Hongyan 2026-10-23 via 序猿助理）。预览未上线，需批准。
-- 编号：050 留给 feat/sel-filter-editable（050_sel_filters）；051 已是 task-reconcile。
-- 行为：worker 把 counting→counted 且 total_count>0 时，DB 触发器等价于 sel_start(run, lim)，
--       lim = min(total_count, 5000)。5000 = 网页 MAX_ROWS / EHunt 只显示前 5,000 条（不是旧 SQL default 100）。
-- 为何用触发器、不改 worker：与 046 auto_send 同思路——count 写库发生在哪台机器（box/ECS）都入队；
--       Hongyan 关掉浏览器也能排队。params（筛选）在 sel_count 时已快照；此处只追加 limit，与手动 sel_start 一致。
-- 并发：037 起 sel_start 已允许并行入队（不再 raise 拦截），只把本 run 设为 queued；本迁移的 core 同样不拦。
-- total_count=0：不入队，保持 counted，网页继续 0 条提示。
-- 可重复执行。

-- 自动上限常量：与 web/js/selscreen.js MAX_ROWS、EHunt 前 5000 条一致。改上限时两边一起改。
-- DEFAULT_AUTO_LIMIT = 5000

create or replace function public.sel_start_core(p_run uuid, p_limit int)
returns boolean language plpgsql security definer set search_path = public as $$
declare r public.sel_runs; lim int; e int;
begin
  -- 只认 counted；已入队/已取消等返回 false（幂等，不报错）
  select * into r from public.sel_runs where id = p_run and status = 'counted' for update;
  if not found then return false; end if;
  if coalesce(r.total_count, 0) <= 0 then return false; end if;
  -- lim = min(请求条数, total_count, 5000)；请求空时用 total_count（自动入队路径）
  lim := least(greatest(coalesce(nullif(p_limit, 0), r.total_count), 1), r.total_count, 5000);
  e := least(lim, coalesce(r.batch_size, 1000));
  update public.sel_runs
     set status = 'queued', queue_at = now(), max_rows = lim, current_batch = 1,
         params = params || jsonb_build_object('limit', lim)
   where id = p_run;
  insert into public.sel_batches (run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end)
  values (p_run, r.user_id, 1, 1, e, 1, (e + 19) / 20);
  return true;
end $$;
revoke all on function public.sel_start_core(uuid, int) from public, anon, authenticated;
grant execute on function public.sel_start_core(uuid, int) to service_role;

-- 网页手动「抓取」：本人 + counted，再走 core（可与自动入队并存；谁先谁赢，后到 false）
create or replace function public.sel_start(p_run uuid, p_limit int default 100)
returns boolean language plpgsql security definer set search_path = public as $$
declare r public.sel_runs;
begin
  select * into r from public.sel_runs where id = p_run and user_id = (select auth.uid()) and status = 'counted' for update;
  if not found then return false; end if;
  -- 不检查并发：有 running/queued 也照常把本 run 排进队尾（037 起即如此）
  return public.sel_start_core(p_run, p_limit);
end $$;
revoke all on function public.sel_start(uuid, int) from public, anon;
grant execute on function public.sel_start(uuid, int) to authenticated;

-- counting→counted 且有结果：自动入队。异常只记，不让 worker 的 counted 写入失败。
create or replace function public.sel_auto_start_on_counted()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if new.status = 'counted' and old.status is distinct from 'counted'
     and coalesce(new.total_count, 0) > 0 then
    begin
      -- 自动条数 = min(total_count, 5000)，见文件头 DEFAULT_AUTO_LIMIT
      perform public.sel_start_core(new.id, least(new.total_count, 5000));
    exception when others then
      update public.sel_runs set error = left('自动入队失败：' || sqlerrm, 300) where id = new.id and status = 'counted';
    end;
  end if;
  return null;
end $$;

drop trigger if exists sel_runs_auto_start on public.sel_runs;
create trigger sel_runs_auto_start
  after update of status on public.sel_runs
  for each row
  when (new.status = 'counted' and old.status is distinct from 'counted')
  execute function public.sel_auto_start_on_counted();
