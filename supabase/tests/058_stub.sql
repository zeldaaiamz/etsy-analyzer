-- 058 本地桩（不是生产结构的全部，只含 058 用到的列）
create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('t.uid', true), '')::uuid $$;
do $$ begin create role authenticated; exception when duplicate_object then null; end $$;
do $$ begin create role anon; exception when duplicate_object then null; end $$;
do $$ begin create role service_role; exception when duplicate_object then null; end $$;
create table public.aba_root_words(id bigint primary key, root_word text, best_rank int, term_count int, root_zh text);
create table public.aba_root_sends(root_id bigint, consumer text, sent_at timestamptz default now(), primary key(root_id, consumer));
create table public.sel_keyword_log(keyword text, queried_at timestamptz default now(), source text);
create table public.sel_runs(id uuid primary key default gen_random_uuid(), keyword text, created_at timestamptz default clock_timestamp());
create or replace function public.aba_check_consumer(p text) returns text language sql as $$ select p $$;
create or replace function public.sel_count(p_keyword text, p_params jsonb) returns uuid language plpgsql as $$
declare i uuid; begin insert into public.sel_keyword_log(keyword, source) values (p_keyword, 'sel_count'); insert into public.sel_runs(keyword) values (p_keyword) returning id into i; return i; end $$;
insert into public.aba_root_words select g, 'w'||g, g, 1, case when g=2 then '词2' end from generate_series(1,15) g;
insert into public.sel_keyword_log(keyword, source) values ('W3 ', 'sel_count');
