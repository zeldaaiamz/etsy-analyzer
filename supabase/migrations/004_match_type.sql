-- 004：amazon_matches 增加匹配类型与相似理由
alter table public.amazon_matches
  add column if not exists match_type text check (match_type in ('identical','similar')),
  add column if not exists similarity_reason text;
