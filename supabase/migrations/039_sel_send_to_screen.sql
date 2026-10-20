-- 039：获取链接「送去预筛选」原子化（Hongyan 2026-10-20 审计修复）。未在生产执行；依赖 024/038（sent_to_screen_at）与预筛选的 batches/jobs。
-- 问题：网页直接写 batches + jobs → 连点重复、>500 条第 2 组被 enforce_job_limits 拒（部分成功）、失败留下空批次、部分成功也标“已发送”、标记写失败用户看不到。
-- 修复：一个事务完成「建预筛选批次（每个 ≤500 条）+ 建 jobs + 标记 sel_batches」，按用户加 advisory lock 串行化；
--   已全部发送的批次默认跳过（连点 / 两个标签页同时点不会再建一份），p_resend = true 才整批重发；
--   排队+运行中上限 1000：先算剩余额度，只发得下的部分（按 rank 顺序），sent_count 记录本批已发送多少条，之后可以「发送剩余」。
alter table public.sel_batches add column if not exists sent_count int;
comment on column public.sel_batches.sent_count is '本批已送去预筛选的链接数（按 rank 顺序的前 N 条）；null 且 sent_to_screen_at 非空 = 038 时代整批已发送';

create or replace function public.sel_send_to_screen(p_batches uuid[], p_resend boolean default false) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  uid uuid := (select auth.uid()); b record; tot int; done0 int; cap int; n int; chunk int; k int; nb int := 0; first_pb uuid; pb uuid;
  res jsonb := '[]'::jsonb; sent_all int := 0; limited boolean := false; seen bigint[] := '{}'; ids bigint[]; urls text[];
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
    n := 0; k := 0; pb := null;
    for i in 1 .. coalesce(array_length(ids, 1), 0) loop
      if ids[i] = any(seen) then n := n + 1; continue; end if;
      if cap <= 0 then limited := true; exit; end if;
      if pb is null or k >= 500 then  -- 每个预筛选批次最多 500 条（enforce_job_limits）
        insert into public.batches (user_id, name) values (uid, format('获取链接 %s 第%s批 %s', b.keyword, b.batch_no, to_char(now() at time zone 'Asia/Shanghai', 'MM-DD HH24:MI'))) returning id into pb;
        nb := nb + 1; first_pb := coalesce(first_pb, pb); k := 0;
      end if;
      insert into public.jobs (batch_id, user_id, etsy_url, etsy_listing_id, prescreen) values (pb, uid, urls[i], ids[i], true);
      seen := seen || ids[i]; k := k + 1; n := n + 1; cap := cap - 1; sent_all := sent_all + 1;
    end loop;
    if n > 0 then update public.sel_batches set sent_to_screen_at = now(), sent_count = done0 + n where id = b.id; end if;
    res := res || jsonb_build_object('batch_id', b.id, 'batch_no', b.batch_no, 'sent', n, 'sent_count', done0 + n, 'total', tot, 'sent_at', case when n > 0 then now() else b.sent_to_screen_at end);
  end loop;
  if sent_all = 0 and limited then raise exception '预筛选排队中+运行中的任务已达上限 1000 条，请等预筛选跑完一些后再发送（这次没有发送任何链接）'; end if;
  return jsonb_build_object('batches', res, 'sent', sent_all, 'screen_batches', nb, 'first_screen_batch', first_pb, 'limited', limited, 'room_left', greatest(cap, 0));
end $$;
revoke all on function public.sel_send_to_screen(uuid[], boolean) from public, anon;
grant execute on function public.sel_send_to_screen(uuid[], boolean) to authenticated;
