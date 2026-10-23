-- 053 本地测试：显式「重新获取」已成功项；042 静默路径仍不重取；额满整批拒绝
\set u1 '11111111-1111-1111-1111-111111111111'
\set u2 '22222222-2222-2222-2222-222222222222'
insert into auth.users values (:'u1'), (:'u2');
create or replace function pg_temp.as_user(u uuid) returns void language sql as $$ select set_config('request.jwt.claim.sub', u::text, false) $$;
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$
begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;

insert into public.batches (id, user_id, name) values
  ('bbbbbbbb-0000-0000-0000-000000000001', :'u1', '获取链接 Gift 第1批'),
  ('bbbbbbbb-0000-0000-0000-000000000002', :'u2', 'other user');

-- u1：3 条已获取(screening)、1 条 kept、1 条 failed、1 条 queued、1 条 hidden screening
insert into public.jobs (id, batch_id, user_id, etsy_url, etsy_listing_id, status, attempts, prescreen, screen_state) values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'bbbbbbbb-0000-0000-0000-000000000001', :'u1', 'https://www.etsy.com/listing/1001', 1001, 'screening', 1, true, 'awaiting'),
  ('aaaaaaaa-0000-0000-0000-000000000002', 'bbbbbbbb-0000-0000-0000-000000000001', :'u1', 'https://www.etsy.com/listing/1002', 1002, 'screening', 1, true, 'awaiting'),
  ('aaaaaaaa-0000-0000-0000-000000000003', 'bbbbbbbb-0000-0000-0000-000000000001', :'u1', 'https://www.etsy.com/listing/1003', 1003, 'screening', 1, true, 'awaiting'),
  ('aaaaaaaa-0000-0000-0000-000000000004', 'bbbbbbbb-0000-0000-0000-000000000001', :'u1', 'https://www.etsy.com/listing/1004', 1004, 'queued', 0, true, 'kept'),
  ('aaaaaaaa-0000-0000-0000-000000000005', 'bbbbbbbb-0000-0000-0000-000000000001', :'u1', 'https://www.etsy.com/listing/1005', 1005, 'failed', 2, true, null),
  ('aaaaaaaa-0000-0000-0000-000000000006', 'bbbbbbbb-0000-0000-0000-000000000001', :'u1', 'https://www.etsy.com/listing/1006', 1006, 'queued', 0, true, null),
  ('aaaaaaaa-0000-0000-0000-000000000007', 'bbbbbbbb-0000-0000-0000-000000000001', :'u1', 'https://www.etsy.com/listing/1007', 1007, 'screening', 1, true, 'awaiting');
update public.jobs set screen_hidden_at = now() where id = 'aaaaaaaa-0000-0000-0000-000000000007';
insert into public.jobs (id, batch_id, user_id, etsy_url, etsy_listing_id, status, attempts, prescreen, screen_state) values
  ('aaaaaaaa-0000-0000-0000-000000000099', 'bbbbbbbb-0000-0000-0000-000000000002', :'u2', 'https://www.etsy.com/listing/2099', 2099, 'screening', 1, true, 'awaiting');

insert into public.etsy_products (job_id, user_id, listing_id, title, image_url) values
  ('aaaaaaaa-0000-0000-0000-000000000001', :'u1', 1001, 'A', 'https://i.etsystatic.com/a.jpg'),
  ('aaaaaaaa-0000-0000-0000-000000000002', :'u1', 1002, 'B', 'https://i.etsystatic.com/b.jpg'),
  ('aaaaaaaa-0000-0000-0000-000000000003', :'u1', 1003, 'C', 'https://i.etsystatic.com/c.jpg'),
  ('aaaaaaaa-0000-0000-0000-000000000004', :'u1', 1004, 'Kept', 'https://i.etsystatic.com/k.jpg');

insert into public.ehunt_lookup_requests (listing_id, status, image_url, attempts, finished_at) values
  (1001, 'done', 'https://i.etsystatic.com/a.jpg', 1, now()),
  (1002, 'done', 'https://i.etsystatic.com/b.jpg', 1, now()),
  (1003, 'done', 'https://i.etsystatic.com/c.jpg', 1, now()),
  (1005, 'error', null, 2, now()),
  (2099, 'done', 'https://i.etsystatic.com/u2.jpg', 1, now());

insert into public.provider_cache (key, provider, payload, expires_at) values
  ('ehunt_web:listing:1001', 'ehunt_web', '{"image_url":"a"}', now() + interval '1 day'),
  ('ehunt:listing:1001', 'ehunt', '{"listing_id":1001}', now() + interval '1 day'),
  ('ehunt_web:listing:1002', 'ehunt_web', '{"image_url":"b"}', now() + interval '1 day'),
  ('ehunt_web:listing:2099', 'ehunt_web', '{"image_url":"u2"}', now() + interval '1 day'),
  ('km:product:30:X', 'keepamore', '{}', now() + interval '1 day');

insert into public.product_library (user_id, job_id, etsy_listing_id) values (:'u1', 'aaaaaaaa-0000-0000-0000-000000000004', 1004);
insert into public.sel_keyword_log (keyword) values ('Gift');

select pg_temp.as_user(:'u1');

select pg_temp.ok((public.screen_refetch_got('{}'::uuid[]))->>'refetched' = '0', '空数组 → 0');

select public.screen_refetch_got(array[
  'aaaaaaaa-0000-0000-0000-000000000001'::uuid,
  'aaaaaaaa-0000-0000-0000-000000000002'::uuid,
  'aaaaaaaa-0000-0000-0000-000000000004'::uuid,
  'aaaaaaaa-0000-0000-0000-000000000005'::uuid,
  'aaaaaaaa-0000-0000-0000-000000000006'::uuid,
  'aaaaaaaa-0000-0000-0000-000000000007'::uuid,
  'aaaaaaaa-0000-0000-0000-000000000099'::uuid
]) as r \gset
select pg_temp.ok((:'r'::jsonb)->>'refetched' = '2', '只重新获取 2 条已获取');
select pg_temp.ok((:'r'::jsonb)->>'lookups' = '2', '2 条 lookup done→queued');
select pg_temp.ok(((:'r'::jsonb)->>'caches')::int >= 3, '清掉对应 provider_cache（≥3）');
select pg_temp.ok((:'r'::jsonb)->>'products_cleared' = '2', '清掉 2 条 etsy_products');

select pg_temp.ok((select status::text from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000001') = 'queued'
              and (select attempts from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000001') = 0
              and (select screen_state from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000001') = 'awaiting',
              'job1 → queued/attempts=0/awaiting');
select pg_temp.ok(not exists (select 1 from public.etsy_products where job_id = 'aaaaaaaa-0000-0000-0000-000000000001'), 'job1 旧主图已清');
select pg_temp.ok((select status from public.ehunt_lookup_requests where listing_id = 1001) = 'queued'
              and (select image_url from public.ehunt_lookup_requests where listing_id = 1001) is null,
              'lookup 1001 done→queued 且清 image_url');
select pg_temp.ok(not exists (select 1 from public.provider_cache where key in ('ehunt_web:listing:1001','ehunt:listing:1001','ehunt_web:listing:1002')),
              '1001/1002 缓存已清');
select pg_temp.ok(exists (select 1 from public.provider_cache where key = 'km:product:30:X'), '其它缓存不动');
select pg_temp.ok(exists (select 1 from public.provider_cache where key = 'ehunt_web:listing:2099'), '别人的缓存不动');

select pg_temp.ok((select status::text from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000003') = 'screening', '未选中的 screening 不动');
select pg_temp.ok(exists (select 1 from public.etsy_products where job_id = 'aaaaaaaa-0000-0000-0000-000000000003'), '未选中的主图保留');

select pg_temp.ok((select status::text from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000004') = 'queued'
              and (select screen_state from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000004') = 'kept'
              and exists (select 1 from public.etsy_products where job_id = 'aaaaaaaa-0000-0000-0000-000000000004'),
              'kept 不动（含主图）');
select pg_temp.ok((select status::text from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000005') = 'failed', 'failed 不动（仍走一键刷新）');
select pg_temp.ok((select status::text from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000006') = 'queued', '取数中不动');
select pg_temp.ok((select status::text from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000007') = 'screening'
              and (select screen_hidden_at is not null from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000007'),
              '已隐藏的 screening 不动');
select pg_temp.ok((select status::text from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000099') = 'screening'
              and (select status from public.ehunt_lookup_requests where listing_id = 2099) = 'done',
              '别人的 job / lookup 不动');

select pg_temp.ok((select count(*) from public.product_library) = 1, '产品库行数不变');
select pg_temp.ok((select count(*) from public.sel_keyword_log) = 1 and (select keyword from public.sel_keyword_log) = 'Gift', 'sel_keyword_log 不动');

select pg_temp.ok((public.screen_refetch_got(array['aaaaaaaa-0000-0000-0000-000000000001'::uuid]))->>'refetched' = '0',
              '已排队的不会再被本 RPC 动（防连点）');

-- 额满：先把当前 queued+running 填到 1000，再对一条新的 screening 调用 → 整批拒绝
do $$
declare cur int; need int;
begin
  select count(*) into cur from public.jobs where user_id = '11111111-1111-1111-1111-111111111111' and status in ('queued','running');
  need := greatest(1000 - cur, 0);
  if need > 0 then
    insert into public.jobs (batch_id, user_id, etsy_url, etsy_listing_id, status, prescreen)
    select 'bbbbbbbb-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111',
           'https://www.etsy.com/listing/' || (800000 + g), 800000 + g, 'queued', true
      from generate_series(1, need) g;
  end if;
end $$;

insert into public.jobs (id, batch_id, user_id, etsy_url, etsy_listing_id, status, attempts, prescreen, screen_state)
values ('aaaaaaaa-0000-0000-0000-000000000088', 'bbbbbbbb-0000-0000-0000-000000000001', :'u1', 'https://www.etsy.com/listing/1088', 1088, 'screening', 1, true, 'awaiting');
insert into public.etsy_products (job_id, user_id, listing_id, image_url)
values ('aaaaaaaa-0000-0000-0000-000000000088', :'u1', 1088, 'https://i.etsystatic.com/x.jpg');
insert into public.ehunt_lookup_requests (listing_id, status, image_url, finished_at)
values (1088, 'done', 'https://i.etsystatic.com/x.jpg', now())
on conflict (listing_id) do update set status = 'done', image_url = excluded.image_url, finished_at = now();

do $$
declare ok boolean := false; msg text;
begin
  begin
    perform public.screen_refetch_got(array['aaaaaaaa-0000-0000-0000-000000000088'::uuid]);
  exception when others then
    ok := sqlerrm ~ '上限 1000';
    msg := sqlerrm;
  end;
  if not ok then raise exception 'FAIL: 额满时应拒绝，实际: %', coalesce(msg, '(no error)'); end if;
  raise notice 'ok: 额满拒绝整批';
end $$;
select pg_temp.ok((select status::text from public.jobs where id = 'aaaaaaaa-0000-0000-0000-000000000088') = 'screening', '额满拒绝后 job 仍 screening');
select pg_temp.ok(exists (select 1 from public.etsy_products where job_id = 'aaaaaaaa-0000-0000-0000-000000000088'), '额满拒绝后主图仍在');

select 'ALL 053 TESTS PASSED';
