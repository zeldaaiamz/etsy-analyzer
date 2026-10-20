-- 046（原 feat/auto-send 的 044，改号；045 留给 ECS 心跳、036 留给计数 worker）：获取链接 → 预筛选 自动发送（Hongyan 2026-10-20）。未在生产执行，需 Hongyan 批准后才上线；依赖 039/041/042/044。sel_send_core 合并 042 去重/复用 + 044 恢复被删除的已获取任务（restored，手动和自动都恢复）。
-- 全部在数据库里完成（触发器），与获取链接 worker 跑在 box 还是阿里云 ECS 无关：worker 只照常把 sel_batches.status 改成 'done'。
-- ① sel_prefs.auto_send（每个用户一行，没有行 = 开启，默认 ON）；网页「自动送去预筛选」开关读写本人这一行。
-- ② 批次读完（status → 'done'）：BEFORE 触发器记 auto_wanted（只对 046 之后读完的批次；历史批次不会被自动发送），
--    AFTER 触发器调用 sel_auto_send(user)。发送失败只记 auto_error，绝不让 worker 的状态更新失败。
-- ③ 每条链接只发一次：复用 042 的发送逻辑（本文件把它拆成 sel_send_core(uid, …)；sel_send_to_screen 只是带登录检查的包装，行为不变）：
--    按用户 advisory lock 串行化，已发送的前 sent_count 条（rank 顺序）永远跳过；已在预筛选 / 已获取成功的算已发送（dup / reused），不重复建任务。
--    手动和自动同时发、两个批次同时读完，都在同一把锁下排队，第二个看到的是第一个更新后的 sent_count → 不会重复。
-- ④ 部分发送后自动补发：排队+运行中满 1000 条时只发得下的部分，auto_pending = true；
--    之后预筛选任务离开 queued/running（语句级触发器，先用部分索引判断该用户有没有待补发，没有就直接返回）→ 再发剩余的。
-- ⑤ auto_sent_at：自动发送过（网页显示「已自动发送」）；每条链接的「已发送」勾选仍按 sent_count，自动 / 手动一致。
-- 可重复执行（if not exists / create or replace / drop trigger if exists）。

create table if not exists public.sel_prefs (user_id uuid primary key default auth.uid(), auto_send boolean not null default true, updated_at timestamptz not null default now());
alter table public.sel_prefs enable row level security;
drop policy if exists sel_prefs_own on public.sel_prefs;
create policy sel_prefs_own on public.sel_prefs for all to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
grant select, insert, update on public.sel_prefs to authenticated;

alter table public.sel_batches add column if not exists auto_wanted boolean not null default false;
alter table public.sel_batches add column if not exists auto_pending boolean not null default false;
alter table public.sel_batches add column if not exists auto_sent_at timestamptz;
alter table public.sel_batches add column if not exists auto_error text;
create index if not exists sel_batches_auto_pending_idx on public.sel_batches (user_id) where auto_pending;

create or replace function public.sel_send_core(uid uuid, p_batches uuid[], p_resend boolean, p_auto boolean) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  b record; tot int; done0 int; cap int; n int; k int; rc int; dupn int; dup_all int := 0; ex bigint[]; nb int := 0; first_pb uuid; pb uuid;
  res jsonb := '[]'::jsonb; sent_all int := 0; limited boolean := false; seen bigint[] := '{}'; ids bigint[]; urls text[]; have bigint[]; reused int; reused_all int := 0; rest int; rest_all int := 0; rid uuid;
begin
  if uid is null then return null; end if;
  perform pg_advisory_xact_lock(hashtext('sel_send:' || uid::text));
  select 1000 - count(*) into cap from public.jobs where user_id = uid and status in ('queued','running');
  for b in select sb.*, r.keyword from public.sel_batches sb join public.sel_runs r on r.id = sb.run_id
            where sb.id = any(p_batches) and sb.user_id = uid and sb.status in ('done','canceled','failed') order by r.created_at, sb.batch_no for update of sb loop
    select count(*) into tot from public.sel_items where batch_id = b.id;
    done0 := case when p_resend then 0 when b.sent_to_screen_at is not null then coalesce(b.sent_count, tot) else 0 end;
    if tot = 0 or done0 >= tot then
      if b.auto_pending then update public.sel_batches set auto_pending = false where id = b.id; end if;
      res := res || jsonb_build_object('batch_id', b.id, 'batch_no', b.batch_no, 'sent', 0, 'dup', 0, 'reused', 0, 'restored', 0, 'sent_count', least(done0, tot), 'total', tot, 'skipped', case when tot = 0 then 'empty' else 'already' end, 'sent_at', b.sent_to_screen_at);
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
    if n > 0 then update public.sel_batches set sent_to_screen_at = now(), sent_count = done0 + n,
        auto_sent_at = case when p_auto then now() else auto_sent_at end where id = b.id; end if;
    -- 046：自动发送待补发标记 = 开了自动发送且还有没发完的（额度满停下）；发完 / 手动发完都会清掉
    update public.sel_batches set auto_pending = (auto_wanted and done0 + n < tot) where id = b.id and auto_pending is distinct from (auto_wanted and done0 + n < tot);
    res := res || jsonb_build_object('batch_id', b.id, 'batch_no', b.batch_no, 'sent', n - reused - dupn, 'dup', dupn, 'reused', reused, 'restored', rest, 'sent_count', done0 + n, 'total', tot, 'sent_at', case when n > 0 then now() else b.sent_to_screen_at end);
  end loop;
  if sent_all = 0 and limited and not p_auto then raise exception '预筛选排队中+运行中的任务已达上限 1000 条，请等预筛选跑完一些后再发送（这次没有发送任何链接）'; end if;
  return jsonb_build_object('batches', res, 'sent', sent_all, 'dup', dup_all, 'reused', reused_all, 'restored', rest_all, 'screen_batches', nb, 'first_screen_batch', first_pb, 'limited', limited, 'room_left', greatest(cap, 0));
end $$;
revoke all on function public.sel_send_core(uuid, uuid[], boolean, boolean) from public, anon, authenticated;

-- 网页手动发送：行为同 042 + 044
create or replace function public.sel_send_to_screen(p_batches uuid[], p_resend boolean default false) returns jsonb
language plpgsql security definer set search_path = public as $$
declare uid uuid := (select auth.uid());
begin
  if uid is null then raise exception '需要登录'; end if;
  return public.sel_send_core(uid, p_batches, p_resend, false);
end $$;
revoke all on function public.sel_send_to_screen(uuid[], boolean) from public, anon;
grant execute on function public.sel_send_to_screen(uuid[], boolean) to authenticated;

-- 自动发送本人所有「开了自动发送、读完了、还没发完」的批次；关闭开关时什么都不做
create or replace function public.sel_auto_send(p_user uuid) returns jsonb
language plpgsql security definer set search_path = public as $$
declare ids uuid[]; r jsonb;
begin
  if p_user is null or not coalesce((select auto_send from public.sel_prefs where user_id = p_user), true) then return null; end if;
  select array_agg(id) into ids from public.sel_batches where user_id = p_user and status = 'done' and (auto_pending or (auto_wanted and sent_to_screen_at is null));
  if ids is null then return null; end if;
  r := public.sel_send_core(p_user, ids, false, true);
  return r;
end $$;
revoke all on function public.sel_auto_send(uuid) from public, anon, authenticated;

create or replace function public.sel_batches_auto_mark() returns trigger language plpgsql as $$
begin
  if new.status = 'done' and old.status is distinct from 'done' then new.auto_wanted := true; new.auto_error := null; end if;
  return new;
end $$;
create or replace function public.sel_batches_auto_send() returns trigger language plpgsql security definer set search_path = public as $$
begin
  begin perform public.sel_auto_send(new.user_id);
  exception when others then update public.sel_batches set auto_error = left(sqlerrm, 300) where id = new.id; end;
  return null;
end $$;
drop trigger if exists sel_batches_auto_mark on public.sel_batches;
create trigger sel_batches_auto_mark before update of status on public.sel_batches for each row execute function public.sel_batches_auto_mark();
drop trigger if exists sel_batches_auto_send on public.sel_batches;
create trigger sel_batches_auto_send after update of status on public.sel_batches for each row
  when (new.status = 'done' and old.status is distinct from 'done') execute function public.sel_batches_auto_send();

-- 补发：预筛选任务离开排队/运行（腾出 1000 条额度）→ 有待补发批次的用户再发剩余的
create or replace function public.jobs_auto_topup() returns trigger language plpgsql security definer set search_path = public as $$
declare u uuid;
begin
  for u in select distinct n.user_id from new_rows n join old_rows o on o.id = n.id
            where o.status in ('queued','running') and n.status not in ('queued','running')
              and exists (select 1 from public.sel_batches s where s.user_id = n.user_id and s.auto_pending) loop
    begin perform public.sel_auto_send(u); exception when others then null; end;
  end loop;
  return null;
end $$;
drop trigger if exists jobs_auto_topup on public.jobs;
create trigger jobs_auto_topup after update on public.jobs referencing old table as old_rows new table as new_rows for each statement execute function public.jobs_auto_topup();
