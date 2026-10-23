-- 053：预筛选「重新获取」已成功项（Hongyan 2026-10-23）。未在生产执行；依赖 033 / 042。
-- 背景：042「除了获取失败，不重复获取」挡住了对已取到主图（status=screening）条目的再次取数；
--   screen_refetch / screen_refetch_all（026/027/032）本来就只处理 status='failed'；ehunt_lookup_request
--   对 status='done' 永不重新排队；provider_cache（ehunt_web:listing:<id> / ehunt:listing:<id>）24h 内也会短路。
-- 例外路径（仅本 RPC，且必须用户在预筛选显式选中后点「重新获取」）：
--   ① 只接受本人、未隐藏、prescreen、status='screening'、screen_state 不是 'kept'、未取消的 id；
--      已进入分析（kept）、取数中（queued/running）、失败（failed）一律不动 —— 失败仍走一键刷新。
--   ② 清掉这些 job 的 etsy_products（界面立刻变「读取中」，不留旧主图；不碰 product_library 产品库）。
--   ③ job 回 queued、attempts=0、screen_state='awaiting'（取完仍待确认；attempts=0 避免 pipeline 复用旧结果）。
--   ④ 把对应 listing 的 ehunt_lookup_requests 从 done → queued（042 的「done 永不重排」在此显式例外）。
--   ⑤ 删掉 provider_cache 里 ehunt_web:listing:<id> / ehunt:listing:<id>，否则 ECS worker 24h 内仍直接回旧图。
--   ⑥ 排队+运行上限 1000：这次要入队的条数若放不下则整批拒绝（不静默少做），避免误刷几百条卡死队列。
-- 红线：不删 / 不改 sel_keyword_log；不提供「一键全部重新获取」；不改 auto_send / sel_prefs；不中停 worker。
-- 性能：按 id 更新 + 按 listing 清缓存，无全表扫。

create or replace function public.screen_refetch_got(p_ids uuid[])
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  uid uuid := (select auth.uid());
  room int;
  jids uuid[] := '{}';
  lids bigint[] := '{}';
  n int := 0;
  n_lookup int := 0;
  n_cache int := 0;
  n_prod int := 0;
  want int;
begin
  if uid is null then raise exception '需要登录'; end if;
  if p_ids is null or coalesce(array_length(p_ids, 1), 0) = 0 then
    return jsonb_build_object('refetched', 0, 'ids', '[]'::jsonb, 'lookups', 0, 'caches', 0, 'products_cleared', 0);
  end if;
  perform pg_advisory_xact_lock(hashtext('sel_send:' || uid::text));

  select coalesce(array_agg(j.id order by j.created_at, j.id), '{}'),
         coalesce(array_agg(j.etsy_listing_id order by j.created_at, j.id) filter (where j.etsy_listing_id is not null), '{}')
    into jids, lids
    from public.jobs j
   where j.id = any(p_ids)
     and j.user_id = uid
     and j.prescreen
     and j.screen_hidden_at is null
     and j.status = 'screening'
     and j.screen_state is distinct from 'kept'
     and not j.cancel_requested;

  want := coalesce(array_length(jids, 1), 0);
  if want = 0 then
    return jsonb_build_object('refetched', 0, 'ids', '[]'::jsonb, 'lookups', 0, 'caches', 0, 'products_cleared', 0);
  end if;

  select 1000 - count(*) into room from public.jobs
   where user_id = uid and status in ('queued', 'running');
  if room < want then
    raise exception '预筛选排队中+运行中的任务加上这次重新获取会超过上限 1000 条（还剩 % 个空位，本次符合条件 % 条），请少选一些或等预筛选跑完一些后再试', room, want;
  end if;

  -- 清旧主图（不碰产品库 product_library）
  delete from public.etsy_products where job_id = any(jids);
  get diagnostics n_prod = row_count;

  update public.jobs set
    status = 'queued',
    error = null,
    stage = null,
    finished_at = null,
    locked_by = null,
    locked_at = null,
    queued_at = now(),
    run_after = now(),
    attempts = 0,
    screen_state = 'awaiting'
  where id = any(jids);
  get diagnostics n = row_count;

  -- 042 例外：显式把已成功的主图查询重新排队
  if coalesce(array_length(lids, 1), 0) > 0 then
    update public.ehunt_lookup_requests set
      status = 'queued',
      image_url = null,
      error = null,
      requested_at = now(),
      claimed_by = null,
      claimed_at = null,
      finished_at = null,
      attempts = 0
    where listing_id = any(lids) and status = 'done';
    get diagnostics n_lookup = row_count;

    -- 清 24h provider_cache，否则 worker 短路不走 lookup
    delete from public.provider_cache
     where key in (
       select format('ehunt_web:listing:%s', x) from unnest(lids) as x
       union all
       select format('ehunt:listing:%s', x) from unnest(lids) as x
     );
    get diagnostics n_cache = row_count;
  end if;

  return jsonb_build_object(
    'refetched', n,
    'ids', to_jsonb(jids),
    'lookups', n_lookup,
    'caches', n_cache,
    'products_cleared', n_prod
  );
end $$;
revoke all on function public.screen_refetch_got(uuid[]) from public, anon;
grant execute on function public.screen_refetch_got(uuid[]) to authenticated;
