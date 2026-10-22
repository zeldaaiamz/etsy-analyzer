#!/usr/bin/env bash
# 049 端到端（本地 Postgres，不碰生产）：加入产品库 → pending → 认领 → 写入真实抓取的日期（dry-run JSON）→ 输出各 listing 的评论日期
# 用法：bash supabase/tests/e2e_049_local.sh dryrun.json
set -euo pipefail
J=$(realpath "$1"); cd "$(dirname "$0")/.."; P=$(ls -d /usr/lib/postgresql/*/bin | tail -1); D=$(mktemp -d); PORT=${PORT:-55450}
"$P/initdb" -D "$D" -U postgres >/dev/null; "$P/pg_ctl" -D "$D" -o "-p $PORT -k $D -c listen_addresses=''" -l "$D/log" start >/dev/null
trap '"$P/pg_ctl" -D "$D" stop -m fast >/dev/null; rm -rf "$D"' EXIT
Q() { psql -h "$D" -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -q -tA "$@"; }
Q -f tests/049_stub.sql >/dev/null; Q -f migrations/049_auto_reviews.sql 2>/dev/null
for lid in $(python3 -c "import json,sys; print(' '.join(json.load(open('$J'))))"); do
  Q -c "insert into public.product_library (user_id, etsy_listing_id) values ('11111111-1111-1111-1111-111111111111', $lid)"
  echo "add $lid → $(Q -c "select status from public.listing_scrape_status where listing_id=$lid")"
done
echo "claim → $(Q -c "select string_agg(x::text, ',') from public.claim_review_scrapes(20) x")"
python3 - "$J" > "$D/fin.sql" <<'PY'
import json, sys
for lid, v in json.load(open(sys.argv[1])).items():
    if v["status"] == "ok": print(f"select public.finish_review_scrape({lid}, 'ok', array[{','.join(repr(d) for d in v['dates'])}]::date[], 'etsy_page', null);")
    else: print(f"select public.finish_review_scrape({lid}, 'failed', null, 'etsy_page', {json.dumps(v['error'])!r});")
PY
Q -f "$D/fin.sql" >/dev/null
Q -c "select s.listing_id, s.status, s.attempts, (select count(*) from public.listing_reviews r where r.listing_id = s.listing_id), (select json_agg(review_date order by review_date desc) from public.listing_reviews r where r.listing_id = s.listing_id) from public.listing_scrape_status s where s.listing_id in (select etsy_listing_id from public.product_library) and s.status in ('ok','failed') and s.listing_id > 1000 order by 1"
echo "re-add (another user) → no refetch: $(Q -c "insert into public.product_library (user_id, etsy_listing_id) select '22222222-2222-2222-2222-222222222222', etsy_listing_id from public.product_library where etsy_listing_id > 1000 and user_id = '11111111-1111-1111-1111-111111111111'; select count(*) from public.claim_review_scrapes(20)") claimed"
