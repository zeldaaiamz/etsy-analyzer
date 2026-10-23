-- 054 本地测试：手动排队评论抓取；除了失败不重复获取 ok；选中缩小范围；权限
create or replace function pg_temp.ok(c boolean, m text) returns void language plpgsql as $$ begin if not coalesce(c, false) then raise exception 'FAIL: %', m; end if; raise notice 'ok: %', m; end $$;
create or replace function pg_temp.st(l bigint) returns text language sql as $$ select status from public.listing_scrape_status where listing_id = l $$;
create or replace function pg_temp.j(k text) returns int language sql as $$ select (current_setting('pg_temp.last_q', true)::jsonb ->> k)::int $$;

-- 基线来自 049 stub + 049 迁移：101=ok, 102=failed, 103/104/105/201=pending（201 是前面测试可能加的；本文件独立跑）
-- 本文件假定只跑 stub+049+054，状态：101 ok, 102 failed, 103/104/105 pending；用户 1111 有 101-105

select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', false);

-- 1) 全库排队：failed→pending，ok 不动，已 pending 计入 already_pending
select set_config('pg_temp.last_q', public.queue_library_review_scrapes(null)::text, false);
select pg_temp.ok(pg_temp.j('queued') = 1 and pg_temp.st(102) = 'pending', '全库：failed 102 → pending，queued=1');
select pg_temp.ok(pg_temp.st(101) = 'ok' and pg_temp.j('skipped_ok') = 1, '全库：ok 101 不重抓');
select pg_temp.ok(pg_temp.j('already_pending') >= 3 and pg_temp.j('total') = pg_temp.j('queued') + pg_temp.j('already_pending'), '全库：pending 计入 already_pending；total=queued+already');

-- 2) 再点一次：没有新的 failed，queued=0，already_pending 仍有
select set_config('pg_temp.last_q', public.queue_library_review_scrapes(null)::text, false);
select pg_temp.ok(pg_temp.j('queued') = 0 and pg_temp.j('already_pending') >= 4, '幂等：已 pending 不再 queued');

-- 3) 缺失状态行：插入无状态 listing → 被 insert 为 pending
insert into public.product_library (user_id, etsy_listing_id) values ('11111111-1111-1111-1111-111111111111', 301);
-- 关掉触发器影响：049 触发器会立刻写 pending；删掉状态行模拟「缺行」
delete from public.listing_scrape_status where listing_id = 301;
select set_config('pg_temp.last_q', public.queue_library_review_scrapes(null)::text, false);
select pg_temp.ok(pg_temp.st(301) = 'pending' and pg_temp.j('queued') = 1, '缺行 → insert pending，queued=1');

-- 4) 选中模式：只处理指定 listing；别人的 / 不在库里的忽略
update public.listing_scrape_status set status = 'failed' where listing_id in (102, 104);
select set_config('pg_temp.last_q', public.queue_library_review_scrapes(array[102, 101, 999999]::bigint[])::text, false);
select pg_temp.ok(pg_temp.st(102) = 'pending' and pg_temp.st(104) = 'failed', '选中：只动 102，104 未选不动');
select pg_temp.ok(pg_temp.st(101) = 'ok' and pg_temp.j('skipped_ok') = 1 and pg_temp.j('queued') = 1, '选中：ok 仍跳过；queued 仅 failed');

-- 5) fetching 不动
update public.listing_scrape_status set status = 'fetching', updated_at = now() where listing_id = 104;
select set_config('pg_temp.last_q', public.queue_library_review_scrapes(array[104]::bigint[])::text, false);
select pg_temp.ok(pg_temp.st(104) = 'fetching' and pg_temp.j('skipped_fetching') = 1 and pg_temp.j('queued') = 0, 'fetching 不打断');

-- 6) 空选中 / 空库范围
select set_config('pg_temp.last_q', public.queue_library_review_scrapes(array[]::bigint[])::text, false);
select pg_temp.ok(pg_temp.j('total') >= 0, '空数组 → 视为全库（与 null 同语义：本实现空数组走全库分支）');
-- 明确：空数组 array_length 为 null，走全库；这与「没选中 = 全库」一致

-- 7) 别人不能动
select set_config('request.jwt.claim.sub', '22222222-2222-2222-2222-222222222222', false);
update public.listing_scrape_status set status = 'failed' where listing_id = 102;
select set_config('pg_temp.last_q', public.queue_library_review_scrapes(array[102]::bigint[])::text, false);
select pg_temp.ok(pg_temp.j('queued') = 0 and pg_temp.st(102) = 'failed', '别人产品库外的 listing 不排队');
-- 用户 2222 库里有 103（049 stub）；把 103 弄成 failed 再试
update public.listing_scrape_status set status = 'failed' where listing_id = 103;
select set_config('pg_temp.last_q', public.queue_library_review_scrapes(null)::text, false);
select pg_temp.ok(pg_temp.st(103) = 'pending' and pg_temp.j('queued') >= 1, '用户 2222 只能排队自己库里的');

-- 8) 未登录
select set_config('request.jwt.claim.sub', '', false);
do $$ begin
  begin
    perform public.queue_library_review_scrapes(null);
    raise exception 'FAIL: 未登录应报错';
  exception when raise_exception then
    if sqlerrm like 'FAIL%' then raise; end if;
    if sqlerrm not like '%需要登录%' then raise exception 'FAIL: 未登录错误文案不对: %', sqlerrm; end if;
  end;
  raise notice 'ok: 未登录拒绝';
end $$;

-- 9) 权限
select pg_temp.ok(
  has_function_privilege('authenticated', 'public.queue_library_review_scrapes(bigint[])', 'execute')
  and not has_function_privilege('anon', 'public.queue_library_review_scrapes(bigint[])', 'execute')
  and not has_function_privilege('public', 'public.queue_library_review_scrapes(bigint[])', 'execute'),
  '权限：仅 authenticated');

-- 10) 不碰 sel_keyword_log（表存在时行数不变）
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', false);
create table if not exists public.sel_keyword_log (id bigserial primary key, keyword text not null, created_at timestamptz not null default now());
insert into public.sel_keyword_log (keyword) values ('gift');
select set_config('pg_temp.kw_before', (select count(*)::text from public.sel_keyword_log), false);
select public.queue_library_review_scrapes(null);
select pg_temp.ok((select count(*) from public.sel_keyword_log) = current_setting('pg_temp.kw_before')::int, '不碰 sel_keyword_log');

select 'ALL 054 TESTS PASSED';
