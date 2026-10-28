#!/usr/bin/env bash
# 056 本地 Postgres 测试：bash supabase/tests/run_056_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."
P=$(ls -d /usr/lib/postgresql/*/bin | tail -1)
D=$(mktemp -d)
PORT=${PORT:-55456}
"$P/initdb" -D "$D" -U postgres >/dev/null
"$P/pg_ctl" -D "$D" -o "-p $PORT -k $D -c listen_addresses=''" -l "$D/log" start >/dev/null
trap '"$P/pg_ctl" -D "$D" stop -m fast >/dev/null; rm -rf "$D"' EXIT
Q() { psql -h "$D" -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q -tA "$@"; }
Q -f tests/049_stub.sql
Q -f migrations/049_auto_reviews.sql
Q -f migrations/054_manual_reviews.sql
Q -f tests/056_stub.sql
Q -f migrations/056_reviews_unavailable.sql
Q -f migrations/056_reviews_unavailable.sql   # 幂等
Q -f tests/056_local_pg_test.sql 2>&1 | tee /tmp/t056.log | sed 's/^psql:[^ ]* NOTICE:  /  /'
grep -q 'ALL 056 TESTS PASSED' /tmp/t056.log
