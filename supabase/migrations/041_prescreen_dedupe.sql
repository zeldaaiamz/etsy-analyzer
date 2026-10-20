-- 041：预筛选列表同一 listing 只出现一次（Hongyan 2026-10-20）。未在生产执行；依赖 017/023（screen_state、screen_hidden_at）、039。
-- 生产排查（10-20 09:13）：prescreen jobs 184 条 / 145 个不同 listing，多出 39 条（37 组），全部来自不同批次之间（同批内 0、URL 变体 0）；
--   批次名都是旧格式「获取链接 <ISO 时间>」= 039 之前网页直接写 batches/jobs 的旧发送路径（09:10 / 09:11 同一批 26 条整批发了两次）。
--   039 只在「同一次调用」里去重，p_resend / 不同时间发的批次 / 任务页勾选预筛选 仍会再建一份。
-- 修复：
--   ① jobs BEFORE INSERT 触发器：预筛选任务按 (user_id, 规范化 listing id) 去重 —— listing id 为空时从 URL /listing/<数字> 取
--     （忽略域名、查询串、结尾斜杠等差异）；列表里（未隐藏的预筛选任务，含已进入分析的）已有 → 这一行静默跳过（return null），
--     旧网页 / 任务页批量 insert 也不会整体失败。按用户 advisory lock（与 039 同一个 key）串行，并发也不会重复。
--     不用唯一索引：worker 在取数时会回写 etsy_listing_id（etsy.me 短链）、恢复隐藏（023）都是 update，唯一索引会让这些 update 报错。
--   ② sel_send_to_screen：先排除已在预筛选里的 listing（算作本批已发送，返回 dup 数），不会为全重复的批次建空批次；重发也只补发不在列表里的。
--   ③ 清理已有重复：每组保留 1 条（已进入分析 > 已有数据/已排除 > 运行中 > 排队 > 失败/取消，同级取最早），其余设 screen_hidden_at（隐藏，可恢复，不删除）；
--     其中仍在排队的改为 canceled，避免再消耗 EHunt 额度。生产按 10-20 09:2x 干跑：列表里可见的重复 32 条将被隐藏（都是 screening/awaiting，0 条排队需取消）；另 7 条重复本来就已隐藏。
-- 批次命名不变：'获取链接 <keyword> 第N批 MM-DD HH:MI'。

create or replace function public.jobs_prescreen_dedupe() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if not coalesce(new.prescreen, false) then return new; end if;
  if new.etsy_listing_id is null then new.etsy_listing_id := substring(new.etsy_url from '/listing/([0-9]{1,18})')::bigint; end if;
  if new.etsy_listing_id is null then return new; end if;   -- etsy.me 短链：取数后才有 id，列表端再去重
  perform pg_advisory_xact_lock(hashtext('sel_send:' || new.user_id::text));
  if exists (select 1 from public.jobs j where j.user_id = new.user_id and j.etsy_listing_id = new.etsy_listing_id and j.prescreen and j.screen_hidden_at is null) then
    return null;   -- 已在预筛选列表里：跳过
  end if;
  return new;
end $$;
revoke all on function public.jobs_prescreen_dedupe() from public, anon, authenticated;
drop trigger if exists jobs_a_prescreen_dedupe on public.jobs;   -- 名字以 a_ 开头：先于 jobs_limits 执行，重复的不占 500/1000 额度
create trigger jobs_a_prescreen_dedupe before insert on public.jobs for each row execute function public.jobs_prescreen_dedupe();

-- ③ 清理已有重复（隐藏，不删除）
with r as (
  select id, status, row_number() over (partition by user_id, etsy_listing_id order by
           (screen_state = 'kept') desc nulls last, (status::text in ('screening','succeeded') or screen_state = 'rejected') desc,
           (status::text = 'running') desc, (status::text = 'queued') desc, created_at, id) rn
    from public.jobs where prescreen and screen_hidden_at is null and etsy_listing_id is not null)
update public.jobs j set screen_hidden_at = now(),
       status = case when j.status::text = 'queued' then 'canceled'::public.job_status else j.status end,
       error = case when j.status::text = 'queued' then '重复链接（预筛选里已有同一 listing，041 去重）' else j.error end
  from r where r.id = j.id and r.rn > 1;

create or replace function public.sel_send_to_screen(p_batches uuid[], p_resend boolean default false) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  uid uuid := (select auth.uid()); b record; tot int; done0 int; cap int; n int; k int; nb int := 0; first_pb uuid; pb uuid; rc int; dupn int; dup_all int := 0;
  res jsonb := '[]'::jsonb; sent_all int := 0; limited boolean := false; seen bigint[] := '{}'; ex bigint[]; ids bigint[]; urls text[];
begin
  if uid is null then raise exception '需要登录'; end if;
  perform pg_advisory_xact_lock(hashtext('sel_send:' || uid::text));
  select 1000 - count(*) into cap from public.jobs where user_id = uid and status in ('queued','running');
  for b in select sb.*, r.keyword from public.sel_batches sb join public.sel_runs r on r.id = sb.run_id
            where sb.id = any(p_batches) and sb.user_id = uid and sb.status in ('done','canceled','failed') order by r.created_at, sb.batch_no for update of sb loop
    select count(*) into tot from public.sel_items where batch_id = b.id;
    done0 := case when p_resend then 0 when b.sent_to_screen_at is not null then coalesce(b.sent_count, tot) else 0 end;
    if tot = 0 or done0 >= tot then
      res := res || jsonb_build_object('batch_id', b.id, 'batch_no', b.batch_no, 'sent', 0, 'dup', 0, 'sent_count', least(done0, tot), 'total', tot, 'skipped', case when tot = 0 then 'empty' else 'already' end, 'sent_at', b.sent_to_screen_at);
      continue;
    end if;
    select array_agg(etsy_listing_id order by rank nulls last, id), array_agg(etsy_url order by rank nulls last, id) into ids, urls
      from (select * from public.sel_items where batch_id = b.id order by rank nulls last, id offset done0) x;
    -- 已在预筛选列表里的 listing（任何批次、任何来源）：不再建 job，算作已发送
    select coalesce(array_agg(distinct j.etsy_listing_id), '{}') into ex from public.jobs j
     where j.user_id = uid and j.prescreen and j.screen_hidden_at is null and j.etsy_listing_id = any(ids);
    n := 0; k := 0; pb := null; dupn := 0;
    for i in 1 .. coalesce(array_length(ids, 1), 0) loop
      if ids[i] = any(seen) or ids[i] = any(ex) then n := n + 1; dupn := dupn + 1; continue; end if;
      if cap <= 0 then limited := true; exit; end if;
      if pb is null or k >= 500 then
        insert into public.batches (user_id, name) values (uid, format('获取链接 %s 第%s批 %s', b.keyword, b.batch_no, to_char(now() at time zone 'Asia/Shanghai', 'MM-DD HH24:MI'))) returning id into pb;
        nb := nb + 1; first_pb := coalesce(first_pb, pb); k := 0;
      end if;
      insert into public.jobs (batch_id, user_id, etsy_url, etsy_listing_id, prescreen) values (pb, uid, urls[i], ids[i], true);
      get diagnostics rc = row_count; seen := seen || ids[i]; n := n + 1;
      if rc = 0 then dupn := dupn + 1; continue; end if;   -- 触发器兜底跳过
      k := k + 1; cap := cap - 1; sent_all := sent_all + 1;
    end loop;
    dup_all := dup_all + dupn;
    if n > 0 then update public.sel_batches set sent_to_screen_at = now(), sent_count = done0 + n where id = b.id; end if;
    res := res || jsonb_build_object('batch_id', b.id, 'batch_no', b.batch_no, 'sent', n - dupn, 'dup', dupn, 'sent_count', done0 + n, 'total', tot, 'sent_at', case when n > 0 then now() else b.sent_to_screen_at end);
  end loop;
  if sent_all = 0 and limited then raise exception '预筛选排队中+运行中的任务已达上限 1000 条，请等预筛选跑完一些后再发送（这次没有发送任何链接）'; end if;
  return jsonb_build_object('batches', res, 'sent', sent_all, 'dup', dup_all, 'screen_batches', nb, 'first_screen_batch', first_pb, 'limited', limited, 'room_left', greatest(cap, 0));
end $$;
revoke all on function public.sel_send_to_screen(uuid[], boolean) from public, anon;
grant execute on function public.sel_send_to_screen(uuid[], boolean) to authenticated;
