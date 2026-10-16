#!/usr/bin/env python3
"""把 classify.py 的输出（selection.json）导入 Supabase（024 表）。需 env SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY（不会打印）。
用法: import_run.py <outdir>/selection.json --user <auth.users.id> [--keyword 词] [--dry-run]"""
import json,os,sys,argparse,urllib.request
ap=argparse.ArgumentParser(); ap.add_argument('selection'); ap.add_argument('--user',required=True); ap.add_argument('--keyword'); ap.add_argument('--dry-run',action='store_true')
A=ap.parse_args(); D=json.load(open(A.selection)); M=D['meta']
split=lambda s:[x.strip() for x in (s or '').split(';') if x.strip()]
run={'user_id':A.user,'keyword':A.keyword or M.get('keyword') or 'unknown','params':{k:M.get(k) for k in('mode','runs','optional_custom','model')},
     'stats':{k:M.get(k) for k in('raw_rows','items','vision_calls','usage','seconds','cost_yuan','counts','gate_counts','reasons','disagree_runs')}}
def item(o,run_id): return {'run_id':run_id,'user_id':A.user,'etsy_listing_id':int(o['listing_id']),'ranks':o['ranks'],'title':o['title'],'price_min':o['price_min'],'price_max':o['price_max'],
    'total_sales':o['total_sales'],'sales_7d':o['sales_7d'],'image_url':o.get('image_url_hd') or o['image_url'],'verdict':o['结果'],'reason':o.get('原因'),'note':o.get('备注'),
    'explain':o.get('说明'),'source':o.get('来源'),'material':o.get('材质'),'acrylic_fit':o.get('acrylic_fit') or None,'custom_type':o.get('定制类型'),
    'selling_points':split(o.get('核心卖点')),'occasions':split(o.get('适用场合')),'votes':o.get('votes'),'dup_note':o.get('dup_note'),
    'gate':o.get('gate') or {'保留':'passed','待确认':'pending','排除':'rejected'}[o['结果']],'gate_by':'auto'}
if A.dry_run:
    print(json.dumps({'run':run,'raw_rows':len(D['raw']),'items':len(D['items']),'sample':item(D['items'][0],'<run>')},ensure_ascii=False,indent=1)); sys.exit()
U=os.environ['SUPABASE_URL'].rstrip('/')+'/rest/v1/'; K=os.environ['SUPABASE_SERVICE_ROLE_KEY']
def post(t,body):
    r=urllib.request.Request(U+t,json.dumps(body).encode(),{'apikey':K,'Authorization':'Bearer '+K,'Content-Type':'application/json','Prefer':'return=representation'})
    return json.load(urllib.request.urlopen(r,timeout=60))
rid=post('sel_runs',run)[0]['id']
post('sel_raw_rows',[{'run_id':rid,'rank':r.get('rank'),'etsy_listing_id':int(r['listing_id']),'raw':r} for r in D['raw']])
post('sel_items',[item(o,rid) for o in D['items']])
print('imported run',rid,len(D['items']),'items')
