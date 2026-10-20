-- 042：预筛选「除了获取失败，不重复获取」（Hongyan 2026-10-20）。未在生产执行；依赖 033 / 039。
-- 规则：同一 listing 只有上一次获取失败才允许再获取；成功、排队中、处理中等其它任何状态都不再获取第二次，直接复用已有结果。
--   ① sel_send_to_screen（含 p_resend「再发一次」）：本人已有非失败/取消的预筛选任务的 listing 不再新建任务，计为 reused（已在预筛选里）；
--      只有之前失败/取消的 listing 会重新建任务取数。返回值每批多 reused，总计多 reused；sent 只计真正新建的。
--   ② ehunt_lookup_request：done（已取到主图）永不重新排队（原 033 是 24 小时后重新排队）；queued/running 原样返回；
--      只有失败（error / login_expired / not_found）才重新排队；not_found 仍保留 24 小时内直接复用，避免反复搜同一个不存在的 listing。
--   ③ ehunt_lookup_claim：running 超过 5 分钟（box 中断）不再静默重新认领（那是第二次获取处理中的项），
--      先标记为 error（获取失败：box 处理超时），之后由 ①/② 的失败重试路径按需重新排队。
--   ④ ehunt_lookup_purge：done 行永久保留（只有主图 URL，很小），只清理失败行 → 3 天后不会因为结果被清掉而重新获取。
--   screen_refetch / screen_refetch_all（026/027/032）本来就只处理 status='failed'，不变。
-- 性能：① 每批多一次走 jobs_user_listing_idx(user_id, etsy_listing_id) 的查询；②③④ 无额外开销。

create or replace function public.sel_send_to_screen(p_batches uuid[], p_resend boolean default false) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  uid uuid := (select auth.uid()); b record; tot int; done0 int; cap int; n int; chunk int; k int; nb int := 0; first_pb uuid; pb uuid;
  res jsonb := '[]'::jsonb; sent_all int := 0; limited boolean := false; seen bigint[] := '{}'; ids bigint[]; urls text[]; have bigint[]; reused int; reused_all int := 0;
begin
  if uid is null then raise exception '需要登录'; end if;
  perform pg_advisory_xact_lock(hashtext('sel_send:' || uid::text));
  select 1000 - count(*) into cap from public.jobs where user_id = uid and status in ('queued','running');
  for b in select sb.*, r.keyword from public.sel_batches sb join public.sel_runs r on r.id = sb.run_id
            where sb.id = any(p_batches) and sb.user_id = uid and sb.status in ('done','canceled','failed') order by r.created_at, sb.batch_no for update of sb loop
    select count(*) into tot from public.sel_items where batch_id = b.id;
    done0 := case when p_resend then 0 when b.sent_to_screen_at is not null then coalesce(b.sent_count, tot) else 0 end;
    if tot = 0 or done0 >= tot then
      res := res || jsonb_build_object('batch_id', b.id, 'batch_no', b.batch_no, 'sent', 0, 'sent_count', least(done0, tot), 'total', tot, 'skipped', case when tot = 0 then 'empty' else 'already' end, 'sent_at', b.sent_to_screen_at);
      continue;
    end if;
    -- 按 rank 顺序取本批还没发的；同一次调用里多个批次重复的 listing 只发一次（算作已发送）
    select array_agg(etsy_listing_id order by rank nulls last, id), array_agg(etsy_url order by rank nulls last, id) into ids, urls
      from (select * from public.sel_items where batch_id = b.id order by rank nulls last, id offset done0) x;
    -- 042 除了获取失败，不重复获取：本人已有预筛选任务且不是失败/取消（成功、排队、处理中…）的 listing 直接复用，不再新建任务（不再取数）
    select coalesce(array_agg(distinct j.etsy_listing_id), '{}') into have from public.jobs j
     where j.user_id = uid and j.prescreen and j.etsy_listing_id = any(ids) and j.status::text not in ('failed', 'canceled');
    n := 0; k := 0; pb := null; reused := 0;
    for i in 1 .. coalesce(array_length(ids, 1), 0) loop
      if ids[i] = any(seen) then n := n + 1; continue; end if;
      if ids[i] = any(have) then n := n + 1; reused := reused + 1; seen := seen || ids[i]; continue; end if;
      if cap <= 0 then limited := true; exit; end if;
      if pb is null or k >= 500 then  -- 每个预筛选批次最多 500 条（enforce_job_limits）
        insert into public.batches (user_id, name) values (uid, format('获取链接 %s 第%s批 %s', b.keyword, b.batch_no, to_char(now() at time zone 'Asia/Shanghai', 'MM-DD HH24:MI'))) returning id into pb;
        nb := nb + 1; first_pb := coalesce(first_pb, pb); k := 0;
      end if;
      insert into public.jobs (batch_id, user_id, etsy_url, etsy_listing_id, prescreen) values (pb, uid, urls[i], ids[i], true);
      seen := seen || ids[i]; k := k + 1; n := n + 1; cap := cap - 1; sent_all := sent_all + 1;
    end loop;
    reused_all := reused_all + reused;
    if n > 0 then update public.sel_batches set sent_to_screen_at = now(), sent_count = done0 + n where id = b.id; end if;
    res := res || jsonb_build_object('batch_id', b.id, 'batch_no', b.batch_no, 'sent', n - reused, 'reused', reused, 'sent_count', done0 + n, 'total', tot, 'sent_at', case when n > 0 then now() else b.sent_to_screen_at end);
  end loop;
  if sent_all = 0 and limited then raise exception '预筛选排队中+运行中的任务已达上限 1000 条，请等预筛选跑完一些后再发送（这次没有发送任何链接）'; end if;
  return jsonb_build_object('batches', res, 'sent', sent_all, 'reused', reused_all, 'screen_batches', nb, 'first_screen_batch', first_pb, 'limited', limited, 'room_left', greatest(cap, 0));
end $$;
revoke all on function public.sel_send_to_screen(uuid[], boolean) from public, anon;
grant execute on function public.sel_send_to_screen(uuid[], boolean) to authenticated;

create or replace function public.ehunt_lookup_request(p_listing bigint)
returns public.ehunt_lookup_requests language plpgsql security definer set search_path = public as $$
declare r public.ehunt_lookup_requests;
begin
  -- 除了获取失败，不重复获取：只有 error / login_expired / not_found(超过 24h) 会重新排队；done、queued、running 原样返回
  insert into public.ehunt_lookup_requests (listing_id) values (p_listing)
  on conflict (listing_id) do update set
    status = 'queued', image_url = null, error = null, requested_at = now(), claimed_by = null, claimed_at = null, finished_at = null,
    attempts = case when public.ehunt_lookup_requests.status = 'error' then public.ehunt_lookup_requests.attempts else 0 end
  where public.ehunt_lookup_requests.status in ('error', 'login_expired')
     or (public.ehunt_lookup_requests.status = 'not_found' and coalesce(public.ehunt_lookup_requests.finished_at, public.ehunt_lookup_requests.requested_at) <= now() - interval '24 hours')
  returning * into r;
  if r.listing_id is null then select * into r from public.ehunt_lookup_requests where listing_id = p_listing; end if;
  return r;
end $$;
revoke all on function public.ehunt_lookup_request(bigint) from public, anon, authenticated;
grant execute on function public.ehunt_lookup_request(bigint) to service_role;

create or replace function public.ehunt_lookup_claim(p_worker text, p_limit int default 10)
returns setof public.ehunt_lookup_requests language plpgsql security definer set search_path = public as $$
begin
  -- 处理中超过 5 分钟 = box 中断 → 记为获取失败（不静默再取一次）；失败项之后可按失败规则重新排队
  update public.ehunt_lookup_requests set status = 'error', error = 'box 处理超时（超过 5 分钟未完成）', finished_at = now()
   where status = 'running' and claimed_at < now() - interval '5 minutes';
  return query
  with c as (
    select listing_id from public.ehunt_lookup_requests where status = 'queued'
     order by requested_at for update skip locked limit greatest(1, least(p_limit, 50)))
  update public.ehunt_lookup_requests q
     set status = 'running', claimed_by = p_worker, claimed_at = now(), attempts = q.attempts + 1
    from c where q.listing_id = c.listing_id
  returning q.*;
end $$;
revoke all on function public.ehunt_lookup_claim(text, int) from public, anon, authenticated;
grant execute on function public.ehunt_lookup_claim(text, int) to service_role;

create or replace function public.ehunt_lookup_purge(p_age interval default interval '3 days')
returns int language plpgsql security definer set search_path = public as $$
declare n int;
begin
  -- done（获取成功）永久保留，清掉就会被重新获取；只清理失败行
  delete from public.ehunt_lookup_requests where status in ('error', 'login_expired', 'not_found') and coalesce(finished_at, requested_at) < now() - p_age;
  get diagnostics n = row_count; return n;
end $$;
revoke all on function public.ehunt_lookup_purge(interval) from public, anon, authenticated;
grant execute on function public.ehunt_lookup_purge(interval) to service_role;
