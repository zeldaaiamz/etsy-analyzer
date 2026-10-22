#!/usr/bin/env bash
# 051 本地 Postgres 测试（一次性实例）：bash supabase/tests/run_051_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."; P=$(ls -d /usr/lib/postgresql/*/bin | tail -1); D=$(mktemp -d); PORT=${PORT:-55451}
"$P/initdb" -D "$D" -U postgres >/dev/null; "$P/pg_ctl" -D "$D" -o "-p $PORT -k $D -c listen_addresses=''" -l "$D/log" start >/dev/null
trap '"$P/pg_ctl" -D "$D" stop -m fast >/dev/null; rm -rf "$D"' EXIT
Q() { psql -h "$D" -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q -tA "$@"; }
Q -f tests/051_stub.sql
Q -f migrations/051_task_reconcile.sql
Q -f migrations/051_task_reconcile.sql   # 幂等：重复执行不报错
Q -f tests/051_local_pg_test.sql 2>&1 | tee /tmp/t051.log | sed 's/^psql:[^ ]* NOTICE:  /  /'
