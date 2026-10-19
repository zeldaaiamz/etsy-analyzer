-- 032：EHunt 账号/服务异常导致的取数失败 → 「刷新 / 一键刷新」可恢复，即使已达重试上限。
-- 背景：2026-10-15 21:32（北京时间）起 EHunt 对所有请求返回 HTTP 200 + {"code":500,"message":"查询失败: 'daily_limit'"}，
--   旧 worker 判为永久失败；用户刷新 3 次后 attempts = 3 = max_attempts，026/027 不再允许刷新（28 条卡死）。
-- 规则：失败原因属于 EHunt 服务异常（与商品本身无关）时，刷新会把 attempts 归零后重新排队；其它失败仍遵守 026/027 的上限规则。
--   匹配：新 worker 的 'EHunt 服务异常（…）'，以及旧 worker 的 'EHunt 错误 5xx: …' / '查询失败' / 'daily_limit'。
-- EHunt 仍异常时重新排队也安全：新 worker 会熔断，任务停在队列里不耗尝试次数，恢复后自动取数。
-- 纯替换：函数签名、权限与 026/027 相同；不改表结构、不动数据。
create or replace function public.screen_refetch(p_ids uuid[])
returns int language sql security definer set search_path = public as $$
  with u as (update public.jobs set status = 'queued', error = null, stage = null, finished_at = null,
                    locked_by = null, locked_at = null, queued_at = now(), run_after = now(),
                    attempts = case when coalesce(error, '') ~ '(EHunt 服务异常|EHunt 错误 5[0-9]{2}|查询失败|daily_limit)' then 0 else attempts end
              where id = any(p_ids) and user_id = auth.uid() and prescreen and status = 'failed'
                and screen_state is distinct from 'kept' and not cancel_requested
                and (attempts < max_attempts or coalesce(error, '') ~ '(EHunt 服务异常|EHunt 错误 5[0-9]{2}|查询失败|daily_limit)')
              returning 1)
  select count(*)::int from u;
$$;
revoke all on function public.screen_refetch(uuid[]) from public, anon;
grant execute on function public.screen_refetch(uuid[]) to authenticated;

create or replace function public.screen_refetch_all(p_batch uuid default null)
returns int language sql security definer set search_path = public as $$
  with u as (update public.jobs set status = 'queued', error = null, stage = null, finished_at = null,
                    locked_by = null, locked_at = null, queued_at = now(), run_after = now(),
                    attempts = case when coalesce(error, '') ~ '(EHunt 服务异常|EHunt 错误 5[0-9]{2}|查询失败|daily_limit)' then 0 else attempts end
              where user_id = auth.uid() and prescreen and status = 'failed' and screen_hidden_at is null
                and screen_state is distinct from 'kept' and not cancel_requested
                and (attempts < max_attempts or coalesce(error, '') ~ '(EHunt 服务异常|EHunt 错误 5[0-9]{2}|查询失败|daily_limit)')
                and (p_batch is null or batch_id = p_batch)
              returning 1)
  select count(*)::int from u;
$$;
revoke all on function public.screen_refetch_all(uuid) from public, anon;
grant execute on function public.screen_refetch_all(uuid) to authenticated;

-- 「一键删除失败」不再删除 EHunt 服务异常导致的失败项（它们可刷新恢复，前端也不再把它们算作“已停止”）。
create or replace function public.screen_delete_failed(p_batch uuid default null)
returns jsonb language sql security definer set search_path = public as $$
  with d as (delete from public.jobs
              where user_id = auth.uid() and prescreen and status = 'failed' and attempts >= max_attempts
                and coalesce(error, '') !~ '(EHunt 服务异常|EHunt 错误 5[0-9]{2}|查询失败|daily_limit)'
                and screen_state is distinct from 'kept' and screen_hidden_at is null
                and (p_batch is null or batch_id = p_batch)
              returning id)
  select jsonb_build_object('deleted', count(*), 'ids', coalesce(jsonb_agg(id), '[]'::jsonb)) from d;
$$;
revoke all on function public.screen_delete_failed(uuid) from public, anon;
grant execute on function public.screen_delete_failed(uuid) to authenticated;
