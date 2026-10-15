-- 022：一键删除全部「不考虑」。语义与 remove_from_library（单个“移出产品库”）一致：只删除本人 product_library 中 stage='rejected' 的条目。
-- 没有表外键引用 product_library（listing_reviews / listing_scrape_status / listing_review_manual / review_ocr_jobs 按 listing_id 关联、无 FK，
-- 且评论数据可能被其它阶段或其它用户共用），因此不删它们；任务、分析、匹配、截图也不删（任务会回到“我的任务”）。
create or replace function public.delete_rejected_library() returns int
language plpgsql security definer set search_path = public as $$
declare n int;
begin
  if (select auth.uid()) is null then return 0; end if;
  delete from public.product_library where user_id = (select auth.uid()) and stage = 'rejected';
  get diagnostics n = row_count;
  return n;
end $$;
revoke all on function public.delete_rejected_library() from public, anon;
grant execute on function public.delete_rejected_library() to authenticated;
