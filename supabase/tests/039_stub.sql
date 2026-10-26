-- 本地 Postgres 测试桩（模拟 Supabase：auth.uid() 读 request.jwt.claim.sub；预筛选 batches/jobs 与生产同结构 + enforce_job_limits）
create role anon; create role authenticated; create role service_role;
create schema auth; create table auth.users (id uuid primary key);
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
create type public.job_status as enum ('queued','running','succeeded','failed','canceled');
create table public.batches (id uuid primary key default gen_random_uuid(), user_id uuid not null default auth.uid() references auth.users(id) on delete cascade, name text, created_at timestamptz not null default now());
create table public.jobs (id uuid primary key default gen_random_uuid(), batch_id uuid not null references public.batches(id) on delete cascade, user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  etsy_url text not null check (etsy_url ~* '^https?://(([a-z]+\.)?etsy\.com|etsy\.me)/'), etsy_listing_id bigint, status public.job_status not null default 'queued', attempts int not null default 0, prescreen boolean not null default false, created_at timestamptz not null default now());
create or replace function public.enforce_job_limits() returns trigger language plpgsql security definer set search_path to 'public' as $function$
declare n_batch int; n_active int;
begin
  if auth.uid() is null then return new; end if;
  select count(*) into n_batch from public.jobs where batch_id = new.batch_id;
  if n_batch >= 500 then raise exception '单次最多提交 500 条' using errcode = 'P0001'; end if;
  select count(*) into n_active from public.jobs where user_id = new.user_id and status in ('queued','running');
  if n_active >= 1000 then raise exception '排队中+运行中的任务已达上限 1000 条，请等待完成后再提交' using errcode = 'P0001'; end if;
  return new;
end $function$;
create trigger jobs_limits before insert on public.jobs for each row execute function enforce_job_limits();
create publication supabase_realtime;
