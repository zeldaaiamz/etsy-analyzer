#!/usr/bin/env bash
# 052 本地 Postgres 测试：bash supabase/tests/run_052_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."; P=$(ls -d /usr/lib/postgresql/*/bin | tail -1); D=$(mktemp -d); PORT=${PORT:-55452}
"$P/initdb" -D "$D" -U postgres >/dev/null; "$P/pg_ctl" -D "$D" -o "-p $PORT -k $D -c listen_addresses=''" -l "$D/log" start >/dev/null
trap '"$P/pg_ctl" -D "$D" stop -m fast >/dev/null; rm -rf "$D"' EXIT
Q() { psql -h "$D" -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q -tA "$@"; }
Q -f tests/039_stub.sql
Q -c "alter type public.job_status add value 'screening'; alter table public.jobs add column if not exists screen_state text, add column if not exists screen_hidden_at timestamptz, add column if not exists error text;"
for m in 024 029 030 033 034 035 037 038 039 041 042 043 044 046 047 048 052; do Q -f migrations/${m}_*.sql; done
Q -f tests/052_local_pg_test.sql 2>&1 | tee /tmp/t052.log | sed 's/^psql:[^ ]* NOTICE:  /  /'
