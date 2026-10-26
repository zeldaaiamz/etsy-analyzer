-- 038：获取链接「最近 3 天查找记录」——逐条删除 + 按批次记录送去预筛选状态（Hongyan 2026-10-20）。未在生产执行。
-- 编号：036 留给 ECS 计数 worker（feature/ecs-count 现为 035_sel_count_worker.sql，需改名 036），037 留给并发队列功能。
-- 现状（024）：sel_runs/sel_batches/sel_items 对 authenticated 已 revoke delete、也没有 delete 策略 → 网页不能直接删，需要 security definer RPC。
-- 规则：只能删自己的；进行中/排队/已暂停（count_queued/counting/queued/running/paused）拒绝；与 037 同一定义，038 覆盖为合并版，先停止再删（避免 box worker 正在写的任务被删掉）。
-- 删除 sel_runs 一行，sel_batches / sel_items 通过 on delete cascade 一起删除。box 本地排除日志不受影响（3 天自动清理）。
create or replace function public.sel_delete_run(p_run uuid) returns boolean
language plpgsql security definer set search_path = public as $$
declare st text;
begin
  select status into st from public.sel_runs where id = p_run and user_id = (select auth.uid()) for update;
  if not found then return false; end if;
  if st in ('count_queued','counting','queued','running','paused') then raise exception '进行中或已暂停的任务不能删除，请先停止'; end if;
  delete from public.sel_runs where id = p_run and user_id = (select auth.uid());
  return true;
end $$;
revoke all on function public.sel_delete_run(uuid) from public, anon;
grant execute on function public.sel_delete_run(uuid) to authenticated;

-- 送去预筛选状态（按批次，Hongyan 2026-10-20）：预筛选的 jobs/batches 与 sel_* 没有关联，无法反查 → 在 sel_batches 记发送时间。
-- 网页「送去预筛选（第 N 批）」提交成功后调用 sel_mark_sent；历史列表显示最近发送的批次（如「第二批已发送」），从未发送 →「未发送」。
-- 不回填：现有数据全部视为未发送（Hongyan 确认）。“复制全部批次”只复制，不算发送。
alter table public.sel_batches add column if not exists sent_to_screen_at timestamptz;
comment on column public.sel_batches.sent_to_screen_at is '本批通过的链接最近一次送去预筛选的时间（网页 sel_mark_sent 写入）；空 = 未发送';
create or replace function public.sel_mark_sent(p_batches uuid[]) returns int
language sql security definer set search_path = public as $$
  with u as (update public.sel_batches set sent_to_screen_at = now()
              where id = any(p_batches) and user_id = (select auth.uid()) and status in ('done','canceled','failed') returning 1)
  select count(*)::int from u;
$$;
revoke all on function public.sel_mark_sent(uuid[]) from public, anon;
grant execute on function public.sel_mark_sent(uuid[]) to authenticated;
