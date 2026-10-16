-- 031：获取链接标题排除词表（后台可编辑）。Hongyan 2026-10-16 定稿：
--   定制类新增 address / return address（整词）；不加 adjust/adjustable；不新增素材/非成品类词；wholesale / bulk / vintage 不排除（原 digital 规则里的 bulk 已去掉）。
-- word 是正则片段，worker 拼成 \b(…)\b 不区分大小写；worker 每 5 分钟加载，表为空/不可用时用 classify.py 内置兜底（与本种子一致）。
create table if not exists public.sel_exclude_words (
  id bigint generated always as identity primary key,
  category text not null check (category in ('custom','ip','digital')),
  word text not null,
  enabled boolean not null default true,
  note text,
  updated_at timestamptz not null default now(),
  updated_by uuid,
  unique (category, word),
  check (lower(trim(word)) not in ('wholesale','bulk','vintage'))
);
create table if not exists public.sel_admins (user_id uuid primary key references auth.users(id) on delete cascade);
alter table public.sel_exclude_words enable row level security;
alter table public.sel_admins enable row level security;
create policy sel_exclude_words_read on public.sel_exclude_words for select to authenticated using (true);
create policy sel_admins_self on public.sel_admins for select to authenticated using (user_id = (select auth.uid()));
revoke insert, update, delete on public.sel_exclude_words, public.sel_admins from authenticated, anon;

create or replace function public.sel_is_admin() returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.sel_admins where user_id = (select auth.uid()));
$$;
-- 新增/修改/停用（只有 sel_admins 里的人可以）。p_id 为空=新增；p_word 必须是合法正则
create or replace function public.sel_upsert_exclude_word(p_id bigint, p_category text, p_word text, p_enabled boolean default true, p_note text default null)
returns bigint language plpgsql security definer set search_path = public as $$
declare rid bigint;
begin
  if not public.sel_is_admin() then raise exception '只有管理员可以修改排除词'; end if;
  if coalesce(trim(p_word), '') = '' then raise exception '排除词不能为空'; end if;
  if lower(trim(p_word)) in ('wholesale','bulk','vintage') then raise exception 'wholesale / bulk / vintage 不排除'; end if;
  perform regexp_match('', trim(p_word));  -- 非法正则会报错
  if p_id is null then
    insert into public.sel_exclude_words (category, word, enabled, note, updated_by) values (p_category, trim(p_word), coalesce(p_enabled, true), p_note, auth.uid()) returning id into rid;
  else
    update public.sel_exclude_words set category = p_category, word = trim(p_word), enabled = coalesce(p_enabled, true), note = p_note, updated_at = now(), updated_by = auth.uid()
     where id = p_id returning id into rid;
  end if;
  return rid;
end $$;
create or replace function public.sel_delete_exclude_word(p_id bigint) returns boolean language plpgsql security definer set search_path = public as $$
begin
  if not public.sel_is_admin() then raise exception '只有管理员可以修改排除词'; end if;
  delete from public.sel_exclude_words where id = p_id; return found;
end $$;
revoke all on function public.sel_is_admin(), public.sel_upsert_exclude_word(bigint, text, text, boolean, text), public.sel_delete_exclude_word(bigint) from public, anon;
grant execute on function public.sel_is_admin(), public.sel_upsert_exclude_word(bigint, text, text, boolean, text), public.sel_delete_exclude_word(bigint) to authenticated;
grant select on public.sel_exclude_words to service_role;

-- 种子：现有词 + 新增 address / return address
insert into public.sel_exclude_words (category, word, note) values
  ('custom', 'personali[sz]ed?', null),
  ('custom', 'personalisation', null),
  ('custom', 'personalization', null),
  ('custom', 'custom(i[sz]ed)?', null),
  ('custom', 'your (name|text|photo|logo)', null),
  ('custom', 'with (name|photo|names)', null),
  ('custom', 'name', null),
  ('custom', 'names', null),
  ('custom', 'photo(?!\s*(booths?|props?)\b)', null),
  ('custom', 'monogram\w*', null),
  ('custom', 'initials?', null),
  ('custom', 'engraved', null),
  ('custom', 'any text', null),
  ('custom', 'logo', null),
  ('custom', 'return address', 'v2 新增'),
  ('custom', 'address', 'v2 新增'),
  ('ip', 'disney', null),
  ('ip', 'marvel', null),
  ('ip', 'pokemon', null),
  ('ip', 'pok[eé]mon', null),
  ('ip', 'star wars', null),
  ('ip', 'harry potter', null),
  ('ip', 'hogwarts', null),
  ('ip', 'peter rabbit', null),
  ('ip', 'hello kitty', null),
  ('ip', 'sanrio', null),
  ('ip', 'barbie', null),
  ('ip', 'nfl', null),
  ('ip', 'nba', null),
  ('ip', 'mlb', null),
  ('ip', 'taylor swift', null),
  ('ip', 'swiftie', null),
  ('ip', 'bluey', null),
  ('ip', 'stitch', null),
  ('ip', 'mickey', null),
  ('ip', 'minnie', null),
  ('ip', 'frozen', null),
  ('ip', 'anime', null),
  ('ip', 'naruto', null),
  ('ip', 'one piece', null),
  ('ip', 'sailor moon', null),
  ('ip', 'ghibli', null),
  ('ip', 'totoro', null),
  ('ip', 'zelda', null),
  ('ip', 'mario', null),
  ('ip', 'nintendo', null),
  ('ip', 'kpop', null),
  ('ip', 'bts', null),
  ('ip', 'labubu', null),
  ('ip', 'snoopy', null),
  ('ip', 'peanuts', null),
  ('ip', 'grinch', null),
  ('ip', 'sesame street', null),
  ('ip', 'paw patrol', null),
  ('ip', 'super ?bowl', null),
  ('ip', 'eras tour', null),
  ('digital', 'svg', null),
  ('digital', 'stl', null),
  ('digital', 'png', null),
  ('digital', 'pdf', null),
  ('digital', 'digital', null),
  ('digital', 'download', null),
  ('digital', 'printable', null),
  ('digital', 'template', null),
  ('digital', 'blank', null),
  ('digital', 'blanks', null),
  ('digital', 'laser cut file', null),
  ('digital', 'cut file', null)
on conflict (category, word) do nothing;
-- 管理员：部署后手动执行（填 Hongyan 的 auth.users id）：insert into public.sel_admins (user_id) values ('<uuid>');
