#!/usr/bin/env bash
# 041 本地 Postgres 测试（一次性实例）：bash supabase/tests/run_041_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."; P=$(ls -d /usr/lib/postgresql/*/bin | tail -1); D=$(mktemp -d); PORT=${PORT:-55441}
"$P/initdb" -D "$D" -U postgres >/dev/null; "$P/pg_ctl" -D "$D" -o "-p $PORT -k $D -c listen_addresses=''" -l "$D/log" start >/dev/null
trap '"$P/pg_ctl" -D "$D" stop -m fast >/dev/null; rm -rf "$D"' EXIT
Q() { psql -h "$D" -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q -tA "$@"; }
Q -f tests/039_stub.sql
Q -c "alter type public.job_status add value 'screening'; alter table public.jobs add column screen_state text, add column screen_hidden_at timestamptz, add column error text;"
for m in 024 029 030 034 035 037 038 039 041; do Q -f migrations/${m}_*.sql; done
Q -f tests/041_local_pg_test.sql 2>&1 | sed 's/^psql:[^ ]* NOTICE:  /  /'
# 并发：3 个连接同时用旧网页路径（直接 insert jobs）发同样 20 条 → 列表里只有 20 条
U=11111111-1111-1111-1111-111111111111
before=$(Q -c "select count(*) from public.jobs where prescreen and screen_hidden_at is null")
for i in 1 2 3; do Q -c "select set_config('request.jwt.claim.sub','$U',false); with b as (insert into public.batches (user_id,name) values ('$U','获取链接 并发$i') returning id) insert into public.jobs (batch_id,user_id,etsy_url,prescreen) select b.id,'$U','https://www.etsy.com/listing/'||(5000000+g)||'/x?ref=$i',true from b, generate_series(1,20) g;" >/dev/null & done; wait
after=$(Q -c "select count(*) from public.jobs where prescreen and screen_hidden_at is null"); n=$((after-before)); [ "$n" = 20 ] && echo "  ok: 3 个并发旧路径 insert 只进了 1 份（$n 条）" || { echo "FAIL: 并发进了 $n 条"; exit 1; }
