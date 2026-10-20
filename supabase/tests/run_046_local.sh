#!/usr/bin/env bash
# 046 本地 Postgres 测试（一次性实例）：bash supabase/tests/run_046_local.sh
set -euo pipefail  # pipefail：SQL 测试出错时脚本失败
cd "$(dirname "$0")/.."; P=$(ls -d /usr/lib/postgresql/*/bin | tail -1); D=$(mktemp -d); PORT=${PORT:-55446}
"$P/initdb" -D "$D" -U postgres >/dev/null; "$P/pg_ctl" -D "$D" -o "-p $PORT -k $D -c listen_addresses=''" -l "$D/log" start >/dev/null
trap '"$P/pg_ctl" -D "$D" stop -m fast >/dev/null; rm -rf "$D"' EXIT
Q() { psql -h "$D" -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q -tA "$@"; }
Q -f tests/039_stub.sql
Q -c "alter type public.job_status add value 'screening'; alter table public.jobs add column if not exists screen_state text, add column if not exists screen_hidden_at timestamptz, add column if not exists error text;"
for m in 024 029 030 033 034 035 037 038 039 041 042 043 044 046; do Q -f migrations/${m}_*.sql; done
Q -f tests/046_local_pg_test.sql 2>&1 | tee /tmp/t046.log | sed 's/^psql:[^ ]* NOTICE:  /  /'
# 并发：两个批次（20 条中 10 条重复）同时读完（worker 的两个连接，service 身份）+ 同时一个手动发送 → 每个 listing 只有 1 个任务
U=11111111-1111-1111-1111-111111111111
Q -c "update public.jobs set status='succeeded';" >/dev/null
Q -c "insert into public.sel_runs (id,user_id,keyword,status) values ('cccccccc-0000-0000-0000-000000000001','$U','conc','running'),('cccccccc-0000-0000-0000-000000000002','$U','conc2','running');
 insert into public.sel_batches (id,run_id,user_id,batch_no,offset_start,offset_end,page_start,page_end,status) values
 ('dddddddd-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','$U',1,1,20,1,1,'running'),('dddddddd-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000002','$U',1,1,20,1,1,'running');
 insert into public.sel_items (run_id,batch_id,user_id,batch_no,etsy_listing_id,rank,verdict) select 'cccccccc-0000-0000-0000-000000000001','dddddddd-0000-0000-0000-000000000001','$U',1,7000+g,g,'保留' from generate_series(1,20) g;
 insert into public.sel_items (run_id,batch_id,user_id,batch_no,etsy_listing_id,rank,verdict) select 'cccccccc-0000-0000-0000-000000000002','dddddddd-0000-0000-0000-000000000002','$U',1,7010+g,100+g,'保留' from generate_series(1,20) g;" >/dev/null
Q -c "select pg_sleep(0.2); update public.sel_batches set status='done' where id='dddddddd-0000-0000-0000-000000000001'" >/dev/null & 
Q -c "select pg_sleep(0.2); update public.sel_batches set status='done' where id='dddddddd-0000-0000-0000-000000000002'" >/dev/null &
Q -c "select set_config('request.jwt.claim.sub','$U',false); select pg_sleep(0.2); select public.sel_send_to_screen(array['dddddddd-0000-0000-0000-000000000001'::uuid,'dddddddd-0000-0000-0000-000000000002'::uuid]);" >/dev/null 2>&1 &
wait
dup=$(Q -c "select count(*) from (select etsy_listing_id from public.jobs where etsy_listing_id between 7001 and 7030 group by 1 having count(*)>1) x")
tot=$(Q -c "select count(distinct etsy_listing_id) from public.jobs where etsy_listing_id between 7001 and 7030")
sc=$(Q -c "select string_agg(sent_count::text, ',' order by run_id) from public.sel_batches where run_id in ('cccccccc-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000002')")
[ "$dup" = 0 ] && [ "$tot" = 30 ] && [ "$sc" = "20,20" ] && echo "  ok: 两个批次同时读完 + 手动发送：30 个 listing 各 1 个任务，sent_count=$sc" || { echo "FAIL: 重复 $dup，listing $tot，sent_count $sc"; exit 1; }
