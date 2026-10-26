#!/usr/bin/env bash
# 055 本地 Postgres 测试：bash supabase/tests/run_055_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."
P=$(ls -d /usr/lib/postgresql/*/bin | tail -1)
D=$(mktemp -d)
PORT=${PORT:-55455}
"$P/initdb" -D "$D" -U postgres >/dev/null
"$P/pg_ctl" -D "$D" -o "-p $PORT -k $D -c listen_addresses=''" -l "$D/log" start >/dev/null
trap '"$P/pg_ctl" -D "$D" stop -m fast >/dev/null; rm -rf "$D"' EXIT
Q() { psql -h "$D" -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q "$@"; }
Q -f tests/055_stub.sql
# 测试文件内 \i 迁移（相对 migrations/）
Q -f tests/055_local_pg_test.sql 2>&1 | tee /tmp/t055.log | sed 's/^psql:[^ ]* NOTICE:  /  /'
grep -q 'ALL 055 TESTS PASSED' /tmp/t055.log
