-- 037 获取链接「抓取队列」（2026-10-20 Hongyan，下载管理器式竖向队列）。未在生产执行；依赖 024/029/030/034/035。
-- （036 预留给 feature/ecs-count。）box 只有 1 个 worker / 1 个浏览器 → 多个任务按顺序排队，一次只抓一个，抓取速度不变。
-- ① 新状态 paused（暂停 = 不再认领；正在抓的批次由 worker 在下一次检查（每 20 行）时停下，已读部分保留，继续时从断点读）
-- ② queue_at：队列顺序（点「抓取」的先后），可上移/下移；认领顺序：超时回收 > 查询数量（几十秒，插队不拖慢抓取）> queue_at
-- ③ 去掉 sel_count / sel_resume / sel_extend 的“已有任务进行中”限制：抓取时也能查询新词、多个任务一起排队（sel_count 只保留防连点）
-- ④ 新 RPC：sel_pause / sel_queue_move / sel_delete_run；sel_resume 兼作「暂停→继续」「失败→刷新/重新抓取」（原地从断点续读，不删不建）
alter table public.sel_runs drop constraint if exists sel_runs_status_check;
alter table public.sel_runs add constraint sel_runs_status_check
  check (status in ('count_queued','counting','counted','queued','running','paused','batch_done','succeeded','failed','canceled'));
alter table public.sel_runs add column if not exists queue_at timestamptz;
comment on column public.sel_runs.queue_at is '抓取队列顺序（越小越先抓）；null 时按 created_at';
drop index if exists sel_runs_queue_idx;
create index if not exists sel_runs_queue_idx on public.sel_runs (status, created_at) where status in ('count_queued','counting','queued','running','paused');

create or replace function public.sel_claim_run(p_worker text) returns public.sel_runs
language plpgsql security definer set search_path = public as $$
declare r public.sel_runs;
begin
  select * into r from public.sel_runs
   where status in ('queued','count_queued') or (status in ('running','counting') and coalesce(heartbeat_at, locked_at) < now() - interval '10 minutes' and attempts < 3)
   order by case when status in ('running','counting') then 0 when status = 'count_queued' then 1 else 2 end, coalesce(queue_at, created_at), created_at
   for update skip locked limit 1;
  if not found then return null; end if;
  update public.sel_runs set status = case when r.status in ('count_queued','counting') then 'counting' else 'running' end,
         locked_by = p_worker, locked_at = now(), heartbeat_at = now(), attempts = attempts + 1,
         started_at = case when r.status in ('count_queued','counting') then started_at else coalesce(started_at, now()) end
   where id = r.id returning * into r;
  return r;
end $$;
revoke all on function public.sel_claim_run(text) from public, anon, authenticated;
grant execute on function public.sel_claim_run(text) to service_role;

create or replace function public.sel_count(p_keyword text, p_params jsonb default '{}'::jsonb)
returns uuid language plpgsql security definer set search_path = public as $$
declare rid uuid; uid uuid := (select auth.uid()); kw text := btrim(regexp_replace(coalesce(p_keyword, ''), '\s+', ' ', 'g'));
begin
  if uid is null or kw = '' then raise exception '需要登录并填写关键词'; end if;
  if length(kw) > 200 then raise exception '关键词太长（最多 200 字）'; end if;
  perform pg_advisory_xact_lock(hashtext('sel_count:' || uid::text));
  if exists (select 1 from public.sel_runs where user_id = uid and status in ('count_queued','counting')) then raise exception '已有正在查询数量的任务'; end if;
  insert into public.sel_runs (user_id, keyword, status, params)
  values (uid, kw, 'count_queued',
          jsonb_build_object('price_min',8,'price_max',40,'min_sales',100,'min_sales_7d',4,'sort','↓7-day sales') || coalesce(p_params, '{}'::jsonb)
          || jsonb_build_object('mode','convertible','runs',3,'optional_custom','keep'))
  returning id into rid;
  return rid;
end $$;

create or replace function public.sel_start(p_run uuid, p_limit int default 100)
returns boolean language plpgsql security definer set search_path = public as $$
declare r public.sel_runs; lim int; e int;
begin
  select * into r from public.sel_runs where id = p_run and user_id = (select auth.uid()) and status = 'counted' for update;
  if not found then return false; end if;
  lim := least(greatest(coalesce(p_limit, 100), 1), coalesce(r.total_count, 5000), 5000);
  e := least(lim, r.batch_size);
  update public.sel_runs set status = 'queued', queue_at = now(), max_rows = lim, current_batch = 1, params = params || jsonb_build_object('limit', lim) where id = p_run;
  insert into public.sel_batches (run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end)
  values (p_run, r.user_id, 1, 1, e, 1, (e + 19) / 20);
  return true;
end $$;

create or replace function public.sel_extend(p_run uuid, p_add int, p_all boolean default false)
returns int language plpgsql security definer set search_path = public as $$
declare r public.sel_runs; uid uuid := (select auth.uid()); s int; e int; newmax int; nb int;
begin
  select * into r from public.sel_runs where id = p_run and user_id = uid and status in ('batch_done','succeeded') for update;
  if not found then return 0; end if;
  select coalesce(max(offset_end), 0) + 1, coalesce(max(batch_no), 0) + 1 into s, nb from public.sel_batches where run_id = p_run;
  newmax := least(s - 1 + greatest(coalesce(p_add, 0), 0), coalesce(r.total_count, 0), 5000);
  if newmax < s then return 0; end if;
  e := least(s + coalesce(r.batch_size, 1000) - 1, newmax);
  insert into public.sel_batches (run_id, user_id, batch_no, offset_start, offset_end, page_start, page_end)
  values (p_run, r.user_id, nb, s, e, (s + 19) / 20, (e + 19) / 20);
  update public.sel_runs set status = 'queued', queue_at = now(), max_rows = newmax, current_batch = nb, read_all = coalesce(p_all, false),
         finished_at = null, error = null, attempts = 0, params = params || jsonb_build_object('limit', newmax)
   where id = p_run;
  return nb;
end $$;

-- 暂停：排队中 → paused（批次保持 queued）；抓取中 → paused，worker 每 20 行检查一次，停下后把批次标记为 canceled（断点保留）
create or replace function public.sel_pause(p_run uuid) returns boolean
language plpgsql security definer set search_path = public as $$
begin
  update public.sel_runs set status = 'paused' where id = p_run and user_id = (select auth.uid()) and status in ('queued','running');
  return found;
end $$;
revoke all on function public.sel_pause(uuid) from public, anon;
grant execute on function public.sel_pause(uuid) to authenticated;

-- 继续 / 重试 / 刷新（原地，从断点续读）：paused → 保持原队列位置；canceled/failed/卡住（running 超过 10 分钟没心跳）→ 排到队尾
create or replace function public.sel_resume(p_run uuid) returns boolean
language plpgsql security definer set search_path = public as $$
declare r public.sel_runs; b public.sel_batches; uid uuid := (select auth.uid());
begin
  select * into r from public.sel_runs where id = p_run and user_id = uid
     and (status in ('canceled','failed','paused') or (status = 'running' and coalesce(heartbeat_at, locked_at) < now() - interval '10 minutes')) for update;
  if not found then return false; end if;
  select * into b from public.sel_batches where run_id = p_run and status in ('canceled','failed','queued','running') order by batch_no desc limit 1 for update;
  if not found then return false; end if;
  update public.sel_batches set status = 'queued', finished_at = null where id = b.id;
  update public.sel_runs set status = 'queued', finished_at = null, error = null, current_batch = b.batch_no, attempts = 0, locked_by = null,
         queue_at = case when r.status = 'paused' then coalesce(queue_at, created_at) else now() end
   where id = p_run;
  return true;
end $$;
revoke all on function public.sel_resume(uuid) from public, anon;
grant execute on function public.sel_resume(uuid) to authenticated;

create or replace function public.sel_cancel(p_run uuid) returns boolean
language plpgsql security definer set search_path = public as $$
begin
  update public.sel_runs set status = 'canceled', finished_at = now()
   where id = p_run and user_id = (select auth.uid()) and status in ('count_queued','counting','counted','queued','running','paused');
  if not found then return false; end if;
  update public.sel_batches set status = 'canceled', finished_at = now() where run_id = p_run and status in ('queued','running');
  return true;
end $$;

-- 上移 / 下移（p_dir = -1 上移，1 下移）：与相邻的等待项（queued/paused，不含正在抓的）交换 queue_at
create or replace function public.sel_queue_move(p_run uuid, p_dir int) returns boolean
language plpgsql security definer set search_path = public as $$
declare uid uuid := (select auth.uid()); a public.sel_runs; o public.sel_runs; ka timestamptz;
begin
  perform pg_advisory_xact_lock(hashtext('sel_queue:' || uid::text));
  select * into a from public.sel_runs where id = p_run and user_id = uid and status in ('queued','paused') for update;
  if not found then return false; end if;
  ka := coalesce(a.queue_at, a.created_at);
  select * into o from public.sel_runs where user_id = uid and id <> p_run and status in ('queued','paused')
     and (case when p_dir < 0 then coalesce(queue_at, created_at) < ka else coalesce(queue_at, created_at) > ka end)
   order by case when p_dir < 0 then extract(epoch from coalesce(queue_at, created_at)) * -1 else extract(epoch from coalesce(queue_at, created_at)) end
   limit 1 for update;
  if not found then return false; end if;
  update public.sel_runs set queue_at = coalesce(o.queue_at, o.created_at) where id = a.id;
  update public.sel_runs set queue_at = ka where id = o.id;
  return true;
end $$;
revoke all on function public.sel_queue_move(uuid, int) from public, anon;
grant execute on function public.sel_queue_move(uuid, int) to authenticated;

-- 删除任务（与「最近 3 天」分支可能的 038 同名同语义：本人、进行中/排队/暂停的先停止，级联删除批次与链接）
create or replace function public.sel_delete_run(p_run uuid) returns boolean
language plpgsql security definer set search_path = public as $$
declare st text;
begin
  select status into st from public.sel_runs where id = p_run and user_id = (select auth.uid()) for update;
  if not found then return false; end if;
  if st in ('count_queued','counting','queued','running','paused') then raise exception '任务进行中，请先停止再删除'; end if;
  delete from public.sel_runs where id = p_run;   -- sel_batches / sel_items on delete cascade
  return true;
end $$;
revoke all on function public.sel_delete_run(uuid) from public, anon;
grant execute on function public.sel_delete_run(uuid) to authenticated;
