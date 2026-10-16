-- 026：预筛选取数失败 →「刷新」重新排队取数。遵守既有重试规则：
--   * 不重置 attempts（与 retry_job/retry_jobs 不同），attempts >= max_attempts（默认 3）时不再排队，前端显示“已达上限”；
--   * 只处理 status='failed' 且 screen_state 不是 'kept' 的预筛任务 → 已保留（进入分析）的产品不会再查 EHunt。
-- 纯追加：新函数 screen_refetch；screen_list 仅在返回的 JSON 里追加 attempts / max_attempts 两个键（签名、过滤与 023 相同）。
create or replace function public.screen_refetch(p_ids uuid[])
returns int language sql security definer set search_path = public as $$
  with u as (update public.jobs set status = 'queued', error = null, stage = null, finished_at = null,
                    locked_by = null, locked_at = null, queued_at = now(), run_after = now()
              where id = any(p_ids) and user_id = auth.uid() and prescreen and status = 'failed'
                and screen_state is distinct from 'kept' and not cancel_requested and attempts < max_attempts
              returning 1)
  select count(*)::int from u;
$$;
revoke all on function public.screen_refetch(uuid[]) from public, anon;
grant execute on function public.screen_refetch(uuid[]) to authenticated;

create or replace function public.screen_list(p_batch uuid default null)
returns jsonb language sql stable security invoker set search_path = public as $$
  select coalesce(jsonb_agg(jsonb_build_object('id', j.id, 'batch_id', j.batch_id, 'etsy_url', j.etsy_url, 'status', j.status,
           'screen_state', j.screen_state, 'error', j.error, 'run_after', j.run_after, 'created_at', j.created_at,
           'attempts', j.attempts, 'max_attempts', j.max_attempts,
           'title', e.title, 'image_url', e.image_url, 'price', e.price, 'currency', e.currency, 'listing_id', coalesce(e.listing_id, j.etsy_listing_id))
           order by j.created_at, j.id), '[]'::jsonb)
    from public.jobs j left join public.etsy_products e on e.job_id = j.id
   where j.user_id = auth.uid() and j.prescreen and coalesce(j.screen_state, '') <> 'kept' and j.screen_hidden_at is null
     and (p_batch is null or j.batch_id = p_batch);
$$;
revoke all on function public.screen_list(uuid) from public, anon;
grant execute on function public.screen_list(uuid) to authenticated;
