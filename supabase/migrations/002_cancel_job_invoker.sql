-- 修复 advisor 0029：cancel_job 改为 SECURITY INVOKER，靠 RLS + 列级权限保证安全
-- 用户只能把自己 queued 状态的任务改成 canceled，且只能改 status/finished_at 两列
revoke update on public.jobs from anon, authenticated;
grant update (status, finished_at) on public.jobs to authenticated;

create policy jobs_cancel on public.jobs for update to authenticated
  using (user_id = (select auth.uid()) and status = 'queued')
  with check (user_id = (select auth.uid()) and status = 'canceled');

create or replace function public.cancel_job(p_job uuid) returns void
language sql security invoker set search_path = public as $$
  update public.jobs set status='canceled', finished_at=now()
   where id = p_job and user_id = (select auth.uid()) and status = 'queued';
$$;
revoke all on function public.cancel_job(uuid) from public, anon;
grant execute on function public.cancel_job(uuid) to authenticated;
