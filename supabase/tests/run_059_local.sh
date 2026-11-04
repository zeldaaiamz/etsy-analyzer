#!/usr/bin/env bash
# 059 本地 Postgres 测试：bash supabase/tests/run_059_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."
P=${PGBIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | tail -1)}
D=$(mktemp -d); PORT=${PORT:-55459}
"$P/initdb" -D "$D" -U postgres >/dev/null
"$P/pg_ctl" -D "$D" -o "-p $PORT -k $D -c listen_addresses=''" -l "$D/log" start >/dev/null
trap '"$P/pg_ctl" -D "$D" stop -m fast >/dev/null; rm -rf "$D"' EXIT
Q() { psql -h "$D" -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q -tA "$@"; }
Q -f tests/058_stub.sql
Q -f migrations/058_aba_pick_to_selection.sql
Q -f migrations/059_aba_seq_picker.sql
Q -f migrations/059_aba_seq_picker.sql   # 幂等
Q -f tests/059_local_pg_test.sql 2>&1 | tee /tmp/t059.log | sed 's/^psql:[^ ]* NOTICE:  /  /'
grep -q 'ALL 059 TESTS PASSED' /tmp/t059.log
