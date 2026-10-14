-- finished_at 兜底：进入终态（succeeded/failed/canceled）时若未写则补 now()；回到 queued/running 时清空。
create or replace function public.jobs_finished_at() returns trigger language plpgsql set search_path = public as $$
begin
  if new.status in ('succeeded','failed','canceled') then
    new.finished_at := coalesce(new.finished_at, now());
  elsif new.status in ('queued','running') then
    new.finished_at := null;
  end if;
  return new;
end $$;
drop trigger if exists jobs_finished_at on public.jobs;
create trigger jobs_finished_at before insert or update of status, finished_at on public.jobs
  for each row execute function public.jobs_finished_at();

-- 回填：已结束但 finished_at 为空的任务，取最佳可用时间
update public.jobs j set finished_at = coalesce(
    (select max(a.created_at) from public.analyses a where a.job_id = j.id), j.updated_at, j.started_at, j.created_at)
 where j.status in ('succeeded','failed','canceled') and j.finished_at is null;
