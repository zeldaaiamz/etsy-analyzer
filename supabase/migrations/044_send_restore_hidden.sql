-- 044：送去预筛选后预筛选页看不到（Hongyan 2026-10-20 11:16）。未在生产执行，需批准。
-- 根因（生产只读核查）：11:14 发送 acrylic keychain 第1批 25 条 → sent=0：21 条 listing 已有 screening/awaiting 任务但在 09:40–09:52 被「删除」（screen_hidden_at），
--   042 把「非失败但已隐藏」计为 reused 且不恢复 → 没建任务、没恢复，预筛选列表 0 条；另 2 条已在列表（dup）。不是刷新问题。
-- 修复：sel_send_to_screen 中 reused 的 listing 若没有可见任务 → 把最新一条非失败的隐藏任务恢复（screen_hidden_at = null），计为 restored。
--   不新建任务、不重新获取、不耗 EHunt 额度；041「同一 listing 只出现一次」不变（只恢复一条，且仅当没有可见任务时）。返回值多 restored。
create or replace function public.sel_send_to_screen(p_batches uuid[], p_resend boolean default false) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  uid uuid := (select auth.uid()); b record; tot int; done0 int; cap int; n int; k int; rc int; dupn int; dup_all int := 0; ex bigint[]; nb int := 0; first_pb uuid; pb uuid;
  res jsonb := '[]'::jsonb; sent_all int := 0; limited boolean := false; seen bigint[] := '{}'; ids bigint[]; urls text[]; have bigint[]; reused int; reused_all int := 0; rest int; rest_all int := 0; rid uuid;
begin
  if uid is null then raise exception '需要登录'; end if;
  perform pg_advisory_xact_lock(hashtext('sel_send:' || uid::text));
  select 1000 - count(*) into cap from public.jobs where user_id = uid and status in ('queued','running');
  for b in select sb.*, r.keyword from public.sel_batches sb join public.sel_runs r on r.id = sb.run_id
            where sb.id = any(p_batches) and sb.user_id = uid and sb.status in ('done','canceled','failed') order by r.created_at, sb.batch_no for update of sb loop
    select count(*) into tot from public.sel_items where batch_id = b.id;
    done0 := case when p_resend then 0 when b.sent_to_screen_at is not null then coalesce(b.sent_count, tot) else 0 end;
    if tot = 0 or done0 >= tot then
      res := res || jsonb_build_object('batch_id', b.id, 'batch_no', b.batch_no, 'sent', 0, 'dup', 0, 'reused', 0, 'sent_count', least(done0, tot), 'total', tot, 'skipped', case when tot = 0 then 'empty' else 'already' end, 'sent_at', b.sent_to_screen_at);
      continue;
    end if;
    -- 按 rank 顺序取本批还没发的；同一次调用里多个批次重复的 listing 只发一次（算作已发送）
    select array_agg(etsy_listing_id order by rank nulls last, id), array_agg(etsy_url order by rank nulls last, id) into ids, urls
      from (select * from public.sel_items where batch_id = b.id order by rank nulls last, id offset done0) x;
    -- 042 除了获取失败，不重复获取：本人已有预筛选任务且不是失败/取消（成功、排队、处理中…）的 listing 直接复用，不再新建任务（不再取数）
    select coalesce(array_agg(distinct j.etsy_listing_id), '{}') into have from public.jobs j
     where j.user_id = uid and j.prescreen and j.etsy_listing_id = any(ids) and j.status::text not in ('failed', 'canceled');
    -- 041 去重：已在预筛选列表里（未隐藏）且不是失败/取消的 listing：不再建 job，算作已发送（dup）
    select coalesce(array_agg(distinct j.etsy_listing_id), '{}') into ex from public.jobs j
     where j.user_id = uid and j.prescreen and j.screen_hidden_at is null and j.etsy_listing_id = any(ids) and j.status::text not in ('failed', 'canceled');
    n := 0; k := 0; pb := null; reused := 0; dupn := 0; rest := 0;
    for i in 1 .. coalesce(array_length(ids, 1), 0) loop
      if ids[i] = any(seen) then n := n + 1; dupn := dupn + 1; continue; end if;
      if ids[i] = any(ex) then n := n + 1; dupn := dupn + 1; seen := seen || ids[i]; continue; end if;
      if ids[i] = any(have) then  -- 044：已有非失败任务但都被隐藏（在预筛选里删除过）→ 恢复最新一条到列表（不重新获取、不耗额度）
        select j.id into rid from public.jobs j where j.user_id = uid and j.prescreen and j.etsy_listing_id = ids[i] and j.screen_hidden_at is not null
           and j.status::text not in ('failed', 'canceled') order by j.created_at desc limit 1;
        if rid is not null then update public.jobs set screen_hidden_at = null where id = rid; rest := rest + 1; end if;
        n := n + 1; reused := reused + 1; seen := seen || ids[i]; continue; end if;
      if cap <= 0 then limited := true; exit; end if;
      if pb is null or k >= 500 then  -- 每个预筛选批次最多 500 条（enforce_job_limits）
        insert into public.batches (user_id, name) values (uid, format('获取链接 %s 第%s批 %s', b.keyword, b.batch_no, to_char(now() at time zone 'Asia/Shanghai', 'MM-DD HH24:MI'))) returning id into pb;
        nb := nb + 1; first_pb := coalesce(first_pb, pb); k := 0;
      end if;
      -- 之前获取失败/取消、仍在列表里的旧任务先隐藏（可恢复），保证列表里同一 listing 只有一条，041 触发器不拦截重新获取
      update public.jobs set screen_hidden_at = now() where user_id = uid and prescreen and screen_hidden_at is null
         and etsy_listing_id = ids[i] and status::text in ('failed', 'canceled');
      insert into public.jobs (batch_id, user_id, etsy_url, etsy_listing_id, prescreen) values (pb, uid, urls[i], ids[i], true);
      get diagnostics rc = row_count; seen := seen || ids[i]; n := n + 1;
      if rc = 0 then dupn := dupn + 1; continue; end if;   -- 041 触发器兜底跳过
      k := k + 1; cap := cap - 1; sent_all := sent_all + 1;
    end loop;
    reused_all := reused_all + reused; rest_all := rest_all + rest; dup_all := dup_all + dupn;
    if n > 0 then update public.sel_batches set sent_to_screen_at = now(), sent_count = done0 + n where id = b.id; end if;
    res := res || jsonb_build_object('batch_id', b.id, 'batch_no', b.batch_no, 'sent', n - reused - dupn, 'dup', dupn, 'reused', reused, 'restored', rest, 'sent_count', done0 + n, 'total', tot, 'sent_at', case when n > 0 then now() else b.sent_to_screen_at end);
  end loop;
  if sent_all = 0 and limited then raise exception '预筛选排队中+运行中的任务已达上限 1000 条，请等预筛选跑完一些后再发送（这次没有发送任何链接）'; end if;
  return jsonb_build_object('batches', res, 'sent', sent_all, 'dup', dup_all, 'reused', reused_all, 'restored', rest_all, 'screen_batches', nb, 'first_screen_batch', first_pb, 'limited', limited, 'room_left', greatest(cap, 0));
end $$;
revoke all on function public.sel_send_to_screen(uuid[], boolean) from public, anon;
grant execute on function public.sel_send_to_screen(uuid[], boolean) to authenticated;

