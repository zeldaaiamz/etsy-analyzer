-- 043 回填（未执行，需 Hongyan 批准；在 043 之后执行，可重复执行）。
-- 来源 A：现存 sel_runs（3 天清理后只剩最近 3 天）——一 run 一条，按 run_id 去重。
insert into public.sel_keyword_log (user_id, keyword, queried_at, run_id, source)
select r.user_id, r.keyword, r.created_at, r.id, 'backfill_sel_runs' from public.sel_runs r
on conflict do nothing;
-- 来源 B：预筛选 batches 名「获取链接 <关键词> 第N批 MM-DD HH:MI」（039 起写入，不随 3 天清理）——
-- 只补 sel_runs 里已不存在的更早查询：每 (用户, 关键词, 自然日) 一条，时间取该日最早的批次创建时间。
insert into public.sel_keyword_log (user_id, keyword, queried_at, run_id, source)
select b.user_id, min(m[1]), min(b.created_at), null, 'backfill_batches'
from public.batches b, regexp_match(b.name, '^获取链接 (.+) 第\d+批') m
where m is not null
  and not exists (select 1 from public.sel_keyword_log l where l.user_id = b.user_id
                   and l.keyword_norm = lower(btrim(regexp_replace(m[1], '\s+', ' ', 'g')))
                   and (l.queried_at at time zone 'Asia/Shanghai')::date = (b.created_at at time zone 'Asia/Shanghai')::date)
group by b.user_id, lower(btrim(regexp_replace(m[1], '\s+', ' ', 'g'))), (b.created_at at time zone 'Asia/Shanghai')::date;
