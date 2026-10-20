-- 038：获取链接「最近 3 天查找记录」逐条删除（Hongyan 2026-10-20）。未在生产执行。
-- 编号：036 留给 ECS 计数 worker（feature/ecs-count 现为 035_sel_count_worker.sql，需改名 036），037 留给并发队列功能。
-- 现状（024）：sel_runs/sel_batches/sel_items 对 authenticated 已 revoke delete、也没有 delete 策略 → 网页不能直接删，需要 security definer RPC。
-- 规则：只能删自己的；进行中（count_queued/counting/queued/running）拒绝，先停止再删（避免 box worker 正在写的任务被删掉）。
-- 删除 sel_runs 一行，sel_batches / sel_items 通过 on delete cascade 一起删除。box 本地排除日志不受影响（3 天自动清理）。
create or replace function public.sel_delete_run(p_run uuid) returns boolean
language plpgsql security definer set search_path = public as $$
declare st text;
begin
  select status into st from public.sel_runs where id = p_run and user_id = (select auth.uid()) for update;
  if not found then return false; end if;
  if st in ('count_queued','counting','queued','running') then raise exception '进行中的任务不能删除，请先停止'; end if;
  delete from public.sel_runs where id = p_run and user_id = (select auth.uid());
  return true;
end $$;
revoke all on function public.sel_delete_run(uuid) from public, anon;
grant execute on function public.sel_delete_run(uuid) to authenticated;
