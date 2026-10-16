import csv,sys,importlib.util
def load(path,name):
    sp=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(sp); sys.argv=['x']; sp.loader.exec_module(m); return m
O=load('/tmp/oldc/classify_old.py','old'); N=load('/workspace/etsy-exclv2/tools/etsy_selection/classify.py','new')
rows=list(csv.DictReader(open('/workspace/amazon/选品/replay/replay_result_v2.csv')))
newx=[];unx=[];chg=[];on=nn=0
for r in rows:
    t=r['title']; o=O.guard({'sales_7d':r['sales_7d']}) or O.rule(t); n=N.guard({'sales_7d':r['sales_7d']}) or N.rule(t)
    on+=bool(o); nn+=bool(n)
    if n and not o: newx.append((r,n))
    elif o and not n: unx.append((r,o))
    elif o and n and o[2]!=n[2]: chg.append((r,o,n))
L=lambda r:f"https://www.etsy.com/listing/{r['listing_id']}"
md=["# 排除词 v2 回放：acrylic decoration 96 条（旧规则 vs 新规则）","",
f"- 数据：`/workspace/amazon/选品/replay/replay_result_v2.csv`（acrylic decoration EHunt 100 行去重后 96 条）",
"- 只比较标题规则这一步（7 天销量兜底 + IP + 定制 + 素材词），不重新调用豆包。",
"- 新规则：定制类加 address / return address（整词）；不加 adjust/adjustable；不新增素材类词；去掉 bulk；wholesale / vintage 不排除。","",
f"| | 旧规则 | 新规则 |\n|---|---|---|\n| 规则直接排除 | {on} | {nn} |\n| 新增排除 | | {len(newx)} |\n| 不再排除 | | {len(unx)} |","",
"## 新增排除（请抽查）",""]
md+=(["| # | 标题 | 链接 | 命中的词 | 旧结果（含豆包） |","|---|---|---|---|---|"]+[f"| {i} | {r['title'].replace('|','/')} | {L(r)} | {n[2][4:]} | {r.get('最终_convertible') or r['系统结果']} {r.get('系统原因','')} |" for i,(r,n) in enumerate(newx,1)]) if newx else ["无：96 条里没有标题含 address / return address 的条目。"]
md+=["","## 不再被规则排除（之后会走豆包判断）",""]
md+=(["| # | 标题 | 链接 | 旧命中词 |","|---|---|---|---|"]+[f"| {i} | {r['title'].replace('|','/')} | {L(r)} | {o[2][4:]} |" for i,(r,o) in enumerate(unx,1)]) if unx else ["无。"]
if chg: md+=["","## 仍排除但命中词变了",""]+[f"- {r['title']} — {o[2]} → {n[2]}" for r,o,n in chg]
# 合成样例，证明新词生效
md+=["","## 规则自检（合成标题，非真实数据）",""]
for t in ["Return Address Stamp Acrylic Block","Acrylic House Number Address Sign","Bulk Acrylic Ornaments Wholesale Lot","Vintage Style Acrylic Sign","Adjustable Acrylic Phone Stand"]:
    n=N.rule(t); md.append(f"- {t} → {('排除，命中 '+n[2][4:]) if n else '不排除'}")
open('/workspace/amazon/选品/排除词v2_回放_acrylic_decoration_96.md','w').write("\n".join(md)+"\n")
print(len(rows),on,nn,len(newx),len(unx),len(chg))
