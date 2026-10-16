#!/usr/bin/env python3
"""selection.json → web/js/demo-selection.js（?demo=1#/select 预览用，不连 Supabase）"""
import json,sys,os
src=sys.argv[1]; D=json.load(open(src)); M=D['meta']
split=lambda s:[x.strip() for x in (s or '').split(';') if x.strip()]
items=[{'id':f"demo-sel-{o['listing_id']}",'etsy_listing_id':o['listing_id'],'ranks':o['ranks'],'title':o['title'],'price_min':o['price_min'],'price_max':o['price_max'],
  'total_sales':o['total_sales'],'sales_7d':o['sales_7d'],'sales_ratio':o['sales_ratio'],'image_url':o.get('image_url_hd') or o['image_url'],'verdict':o['结果'],
  'reason':o.get('原因',''),'note':o.get('备注',''),'explain':o.get('说明',''),'source':o.get('来源'),'material':o.get('材质',''),'acrylic_fit':o.get('acrylic_fit',''),
  'custom_type':o.get('定制类型',''),'selling_points':split(o.get('核心卖点')),'occasions':split(o.get('适用场合')),'votes':o.get('votes',''),'dup_note':o.get('dup_note',''),
  'gate':o.get('gate') or {'保留':'passed','待确认':'pending','排除':'rejected'}[o['结果']],'gate_by':'auto','item_state':'classified','cross_kw':1,'cross_kw_list':[M.get('keyword')],'in_library':False} for o in D['items']]
# 演示跨关键词：前几个保留项假设也出现在另一个词下
for o in [x for x in items if x['verdict']=='保留'][:3]: o['cross_kw']=2; o['cross_kw_list']=[M.get('keyword'),'acrylic cake topper（演示）']
run={'id':'demo-run','keyword':M.get('keyword'),'status':'succeeded','target_count':M.get('raw_rows'),'scraped_count':M.get('raw_rows'),'classified_count':M.get('items'),'created_at':'2026-10-16T03:00:00Z','params':{k:M.get(k) for k in('mode','runs','optional_custom','model')},
     'stats':{k:M.get(k) for k in('raw_rows','items','vision_calls','usage','seconds','cost_yuan','counts','gate_counts','reasons','disagree_runs')}}
out=os.path.join(os.path.dirname(__file__),'..','..','web','js','demo-selection.js')
open(out,'w').write('// 自动生成（tools/etsy_selection/gen_demo.py）：?demo=1#/select 使用的示例数据（acrylic decoration 回放）\nlet FX = null; // 单例：预筛选的人工改动在演示中对选品筛选页可见\nexport const selectionFixtures = () => (FX ||= '+json.dumps({'runs':[run],'items':{'demo-run':items}},ensure_ascii=False)+');\n')
print('wrote',out,len(items))
