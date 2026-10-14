-- 003：人群依据、决策过程、亚马逊证据快照 + Storage
alter table public.analyses
  add column if not exists audience_evidence jsonb,  -- {source, reviews_analyzed, listing_fields_used, method, signals[], evidence[{quote, rating, signal}]}
  add column if not exists decision_trace jsonb;     -- [{step, title, input, output, reasoning}]

alter table public.etsy_products
  add column if not exists shop_id bigint,
  add column if not exists reviews jsonb;            -- 原始评论样本

create table if not exists public.evidence_snapshots (
  id bigserial primary key,
  job_id uuid not null references public.jobs(id) on delete cascade,
  user_id uuid not null,
  kind text not null check (kind in ('identical','similar','search_result')),
  keyword text,
  asin text,
  title text,
  price numeric,
  bsr int,
  review_count int,
  rating numeric,
  similarity numeric,
  similarity_reason text,
  source_url text,
  image_path text,      -- Storage: snapshots/{user_id}/{job_id}/xxx
  image_source text,    -- mock_placeholder | product_image | screenshot
  provider text,
  created_at timestamptz not null default now()
);
create index if not exists evidence_snapshots_job_idx on public.evidence_snapshots(job_id);
alter table public.evidence_snapshots enable row level security;
create policy snapshots_select on public.evidence_snapshots for select to authenticated
  using (user_id = (select auth.uid()));
alter publication supabase_realtime add table public.evidence_snapshots;

-- Storage：私有 bucket，用户只能读自己目录（前端用 signed URL）；只有 service_role 写入
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('snapshots', 'snapshots', false, 10485760, array['image/png','image/jpeg','image/webp','image/svg+xml'])
on conflict (id) do nothing;

create policy snapshots_owner_read on storage.objects for select to authenticated
  using (bucket_id = 'snapshots' and (storage.foldername(name))[1] = (select auth.uid())::text);
