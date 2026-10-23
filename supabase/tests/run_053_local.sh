#!/usr/bin/env bash
# 053 本地 Postgres 测试：bash supabase/tests/run_053_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."
P=$(ls -d /usr/lib/postgresql/*/bin | tail -1)
D=$(mktemp -d)
PORT=${PORT:-55453}
"$P/initdb" -D "$D" -U postgres >/dev/null
"$P/pg_ctl" -D "$D" -o "-p $PORT -k $D -c listen_addresses=''" -l "$D/log" start >/dev/null
trap '"$P/pg_ctl" -D "$D" stop -m fast >/dev/null; rm -rf "$D"' EXIT
Q() { psql -h "$D" -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q -tA "$@"; }
Q -f tests/053_stub.sql
Q -f migrations/053_screen_refetch_got.sql
Q -f migrations/053_screen_refetch_got.sql   # 幂等
Q -f tests/053_local_pg_test.sql 2>&1 | tee /tmp/t053.log | sed 's/^psql:[^ ]* NOTICE:  /  /'
grep -q 'ALL 053 TESTS PASSED' /tmp/t053.log
