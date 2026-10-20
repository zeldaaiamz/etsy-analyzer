#!/usr/bin/env bash
# 039 本地 Postgres 测试（一次性实例）：bash supabase/tests/run_039_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."; P=$(ls -d /usr/lib/postgresql/*/bin | tail -1); D=$(mktemp -d); PORT=${PORT:-55439}
"$P/initdb" -D "$D" -U postgres >/dev/null; "$P/pg_ctl" -D "$D" -o "-p $PORT -k $D -c listen_addresses=''" -l "$D/log" start >/dev/null
trap '"$P/pg_ctl" -D "$D" stop -m fast >/dev/null; rm -rf "$D"' EXIT
Q() { psql -h "$D" -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q -tA "$@"; }
Q -f tests/039_stub.sql
for m in 024 029 030 034 035 037 038 039; do Q -f migrations/${m}_*.sql; done
Q -f tests/039_local_pg_test.sql 2>&1 | sed 's/^psql:[^ ]* NOTICE:  /  /'
# 并发：两个连接同时发同一批（模拟连点 / 两个标签页）→ 只建一份
Q -c "update public.jobs set status='succeeded'; update public.sel_batches set sent_to_screen_at=null, sent_count=null where batch_no=2 and run_id='aaaaaaaa-0000-0000-0000-000000000001';"
before=$(Q -c "select count(*) from public.jobs")
for i in 1 2 3; do Q -c "select set_config('request.jwt.claim.sub','11111111-1111-1111-1111-111111111111',false); select pg_sleep(0.2); select public.sel_send_to_screen(array['bbbbbbbb-0000-0000-0000-000000000002'::uuid])->>'sent';" >/dev/null & done; wait
after=$(Q -c "select count(*) from public.jobs"); n=$((after-before)); [ "$n" = 30 ] && echo "  ok: 3 个并发请求只建了 1 份（$n 条）" || { echo "FAIL: 并发建了 $n 条"; exit 1; }
