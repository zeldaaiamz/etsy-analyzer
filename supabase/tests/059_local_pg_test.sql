do $$ declare n int; a uuid := gen_random_uuid(); b uuid := gen_random_uuid(); begin
  perform set_config('t.uid', '', false);
  begin perform * from public.aba_next_for_selection(10); raise exception 'FAIL 未登录应拒绝'; exception when others then if sqlerrm not like '%需要登录%' then raise; end if; end;
  perform set_config('t.uid', a::text, false);
  -- 第 1 次：1..11 中跳过 w3（已查过）→ 1,2,4..11
  assert (select array_agg(root_id) from public.aba_next_for_selection(10)) = array[1,2,4,5,6,7,8,9,10,11]::bigint[], '第一批';
  assert (select count(*) from public.sel_aba_shown where user_id=a) = 10, '打开即记为出现过';
  -- 只导入 1、2；其余没选 → 第 2 次出不一样的词 12..15（到底只剩 4 个）
  perform * from public.aba_import_selected(array[1,2]::bigint[]);
  assert (select array_agg(root_id) from public.aba_next_for_selection(10)) = array[12,13,14,15]::bigint[], '第二批不重复';
  select count(*) into n from public.aba_next_for_selection(10); assert n = 0, '到底返回空';
  -- 另一个用户有自己的游标，但已导入/已查过的仍跳过
  perform set_config('t.uid', b::text, false);
  assert (select array_agg(root_id) from public.aba_next_for_selection(3)) = array[4,5,6]::bigint[], '用户 b 独立游标';
  assert (select count(*) from public.sel_keyword_log) = 3, 'kwlog 只增不删';
  raise notice 'ALL 059 TESTS PASSED';
end $$;
