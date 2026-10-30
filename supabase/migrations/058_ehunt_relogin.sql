-- 058：EHunt 自动重新登录（Hongyan 2026-10-30 同意方案；预览未上线，需批准）。编号：050 保留给 sel-filter-editable，057 已是 aba_import。
-- 为何需要新迁移：现有可复用表里 service_status（单行 id=1，authenticated 只读，已在 realtime）最合适放“登录状态”，
--   但缺列；sel_prefs 是按用户的偏好行，不适合放全局 worker 请求。所以只给 service_status 加列 + 2 个 RPC，不建新表。
-- ① service_status.ehunt_login_*：worker（service_role）写登录状态 ok / relogging / needs_manual / cooldown + 中文说明，网页读取显示。
-- ② ehunt_request_relogin()：网页「重新登录 EHunt」按钮（不输入密码），只写请求时间；worker 看到后执行一次自动登录并回报。
-- ③ ehunt_requeue_login_failed()：自动登录成功后 worker 调用，只把“因登录失效而失败”的任务重新排队（登录失效属获取失败，可重排；
--    其他失败/成功的一律不动 →「除了获取失败，不重复获取」）。
-- 红线：不改 sel_prefs / auto_send；不删任何数据；不存任何账号密码（凭据只在 box 本地文件）。可重复执行。
alter table public.service_status add column if not exists ehunt_login_state text;          -- ok | relogging | needs_manual | cooldown
alter table public.service_status add column if not exists ehunt_login_msg text;
alter table public.service_status add column if not exists ehunt_login_at timestamptz;       -- worker 最近一次回报时间
alter table public.service_status add column if not exists ehunt_relogin_requested_at timestamptz;
alter table public.service_status add column if not exists ehunt_relogin_requested_by uuid;

create or replace function public.ehunt_request_relogin() returns timestamptz
language plpgsql security definer set search_path = public as $$
declare t timestamptz := now();
begin
  if (select auth.uid()) is null then raise exception '未登录'; end if;
  insert into public.service_status (id) values (1) on conflict do nothing;
  -- 60 秒内重复点击只算一次（worker 另有 30 分钟最多 2 次的冷却上限）
  update public.service_status set ehunt_relogin_requested_at = t, ehunt_relogin_requested_by = (select auth.uid()), updated_at = t
   where id = 1 and (ehunt_relogin_requested_at is null or ehunt_relogin_requested_at < t - interval '60 seconds');
  return (select ehunt_relogin_requested_at from public.service_status where id = 1);
end $$;
revoke all on function public.ehunt_request_relogin() from public, anon;
grant execute on function public.ehunt_request_relogin() to authenticated;

create or replace function public.ehunt_requeue_login_failed() returns jsonb
language plpgsql security definer set search_path = public as $$
declare pat text := 'EHunt 登录已失效|EHunt 未登录'; nc int := 0; nb int := 0; nl int := 0; r record; b public.sel_batches;
begin
  -- 查询数量阶段失败（failed 且还没有数量）→ 重新查询
  update public.sel_runs set status = 'count_queued', error = null, finished_at = null, locked_by = null, queue_at = now()
   where status = 'failed' and total_count is null and error ~ pat and created_at > now() - interval '3 days';
  get diagnostics nc = row_count;
  -- 抓取批次因登录失效中断（canceled + 登录错误）→ 同 sel_resume：最近一个已停止批次重新排队，从断点继续
  for r in select id from public.sel_runs where status = 'canceled' and error ~ pat and created_at > now() - interval '3 days' for update loop
    select * into b from public.sel_batches where run_id = r.id and status = 'canceled' order by batch_no desc limit 1 for update;
    if not found then continue; end if;
    update public.sel_batches set status = 'queued', finished_at = null where id = b.id;
    update public.sel_runs set status = 'queued', finished_at = null, error = null, locked_by = null, current_batch = b.batch_no, attempts = 0, queue_at = now() where id = r.id;
    nb := nb + 1;
  end loop;
  -- 预筛选主图查询
  update public.ehunt_lookup_requests set status = 'queued', error = null, finished_at = null, claimed_by = null, claimed_at = null where status = 'login_expired';
  get diagnostics nl = row_count;
  return jsonb_build_object('count', nc, 'batch', nb, 'lookup', nl);
end $$;
revoke all on function public.ehunt_requeue_login_failed() from public, anon, authenticated;
grant execute on function public.ehunt_requeue_login_failed() to service_role;
