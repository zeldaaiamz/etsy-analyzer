-- 012：任务列表视图 task_jobs = jobs 去掉“已在本人产品库（任一阶段）”的 listing（纯追加，旧前端不受影响）
-- security_invoker：沿用 jobs / product_library / etsy_products 的 RLS，只能看到本人数据
create or replace view public.task_jobs with (security_invoker = true) as
select j.* from public.jobs j
 where not exists (
   select 1 from public.product_library l
    where l.user_id = j.user_id
      and l.etsy_listing_id = coalesce(j.etsy_listing_id, (select e.listing_id from public.etsy_products e where e.job_id = j.id limit 1)));
revoke all on public.task_jobs from anon;
grant select on public.task_jobs to authenticated;
