-- 033 预筛选取数回退：EHunt API 无订阅（免费 200 credits 已用完，2026-10-15 起 code 500 "查询失败: 'daily_limit'"）
-- → ECS worker（ETSY_PROVIDER=ehunt_web）把 listing id 写进请求表，box 上已登录 EHunt 网页的 selection worker
--   在同一浏览器 profile 里按 listing id 搜索，只读取【主图 URL】一项（不读标题/价格/销量/收藏/评论），写回本表。
-- 只有 service_role（ECS worker / box worker）可读写；前端不直接访问。
create table if not exists public.ehunt_lookup_requests (
  listing_id   bigint primary key,
  status       text not null default 'queued'
               check (status in ('queued', 'running', 'done', 'not_found', 'login_expired', 'error')),
  image_url    text,                     -- 唯一读取的数据：主图（il_570xN）
  error        text,
  attempts     int  not null default 0,
  requested_at timestamptz not null default now(),
  claimed_by   text,
  claimed_at   timestamptz,
  finished_at  timestamptz
);
create index if not exists ehunt_lookup_requests_queue on public.ehunt_lookup_requests (status, requested_at);
alter table public.ehunt_lookup_requests enable row level security;   -- 无 policy：anon/authenticated 一律不可见
revoke all on table public.ehunt_lookup_requests from anon, authenticated;
grant select, insert, update, delete on table public.ehunt_lookup_requests to service_role;

-- ECS：请求一条（幂等）。24 小时内已有结果（done/not_found）直接返回；排队/处理中原样返回；
-- 其余（login_expired / error / 过期结果）重置为 queued 重新排队。
create or replace function public.ehunt_lookup_request(p_listing bigint)
returns public.ehunt_lookup_requests language plpgsql security definer set search_path = public as $$
declare r public.ehunt_lookup_requests;
begin
  insert into public.ehunt_lookup_requests (listing_id) values (p_listing)
  on conflict (listing_id) do update set
    status = 'queued', image_url = null, error = null, requested_at = now(), claimed_by = null, claimed_at = null, finished_at = null,
    attempts = case when public.ehunt_lookup_requests.status = 'error' then public.ehunt_lookup_requests.attempts else 0 end
  where not (public.ehunt_lookup_requests.status in ('queued', 'running')
             or (public.ehunt_lookup_requests.status in ('done', 'not_found') and public.ehunt_lookup_requests.finished_at > now() - interval '24 hours'))
  returning * into r;
  if r.listing_id is null then select * into r from public.ehunt_lookup_requests where listing_id = p_listing; end if;
  return r;
end $$;
revoke all on function public.ehunt_lookup_request(bigint) from public, anon, authenticated;
grant execute on function public.ehunt_lookup_request(bigint) to service_role;

-- box：认领最多 p_limit 条（先到先得；running 超过 5 分钟未完成视为 box 中断，重新认领，最多 5 次）。
create or replace function public.ehunt_lookup_claim(p_worker text, p_limit int default 10)
returns setof public.ehunt_lookup_requests language plpgsql security definer set search_path = public as $$
begin
  return query
  with c as (
    select listing_id from public.ehunt_lookup_requests
     where status = 'queued' or (status = 'running' and claimed_at < now() - interval '5 minutes' and attempts < 5)
     order by requested_at for update skip locked limit greatest(1, least(p_limit, 50)))
  update public.ehunt_lookup_requests q
     set status = 'running', claimed_by = p_worker, claimed_at = now(), attempts = q.attempts + 1
    from c where q.listing_id = c.listing_id
  returning q.*;
end $$;
revoke all on function public.ehunt_lookup_claim(text, int) from public, anon, authenticated;
grant execute on function public.ehunt_lookup_claim(text, int) to service_role;

-- 清理：结果保留 3 天（结果本身另有 provider_cache 24 小时缓存）；box worker 每 24 小时调用一次。
create or replace function public.ehunt_lookup_purge(p_age interval default interval '3 days')
returns int language plpgsql security definer set search_path = public as $$
declare n int;
begin
  delete from public.ehunt_lookup_requests where status not in ('queued', 'running') and coalesce(finished_at, requested_at) < now() - p_age;
  get diagnostics n = row_count; return n;
end $$;
revoke all on function public.ehunt_lookup_purge(interval) from public, anon, authenticated;
grant execute on function public.ehunt_lookup_purge(interval) to service_role;
