do $$ declare n int; begin
  begin perform * from public.aba_preview_for_selection(10); raise exception 'FAIL 未登录应拒绝'; exception when others then if sqlerrm not like '%需要登录%' then raise; end if; end;
  perform set_config('t.uid', gen_random_uuid()::text, false);
  select count(*) into n from public.aba_preview_for_selection(10); assert n = 10, 'preview 10';
  assert (select count(*) from public.aba_root_sends) = 0, '预览不写 sends';
  assert (select root_zh from public.aba_preview_for_selection(10) where root_id=2) = '词2', 'root_zh';
  assert (select seen_count from public.aba_preview_for_selection(10) where root_id=3) = 1, 'seen_count 大小写空格';
  -- 选 8 个，乱序传入 → 按传入顺序建任务
  assert (select array_agg(keyword order by ord) from (select keyword, row_number() over () ord from public.aba_import_selected(array[8,1,2,3,4,5,6,7]::bigint[])) x) = array['w8','w1','w2','w3','w4','w5','w6','w7'], '顺序';
  assert (select count(*) from public.sel_runs) = 8, '8 个任务';
  assert (select count(*) from public.aba_root_sends) = 8, '只有选中的写 sends';
  -- 未选的 9、10 下次仍出现；已导入的不再出现
  assert (select array_agg(root_id order by root_id) from public.aba_preview_for_selection(10)) = array[9,10,11,12,13,14,15]::bigint[], '剩余';
  -- 重复导入：跳过
  select count(*) into n from public.aba_import_selected(array[1,1,9]::bigint[]); assert n = 1, '去重';
  assert (select count(*) from public.sel_keyword_log) = 10, 'kwlog 只增不删';
  begin perform * from public.aba_import_selected(array[]::bigint[]); raise exception 'FAIL'; exception when others then if sqlerrm not like '%至少选择%' then raise; end if; end;
  raise notice 'ALL 058 TESTS PASSED';
end $$;
