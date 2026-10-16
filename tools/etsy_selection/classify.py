#!/usr/bin/env python3
"""Etsy 选品自动筛选（已审定默认：convertible + --optional-custom=keep + temperature 0 + 3 次投票，非一致→待确认）。
用法: replay_classify.py <list.json> <outdir> [--baseline baseline.csv] [--mode convertible|lenient|strict] [--runs 3] [--optional-custom keep|exclude] [--keyword 词]
说明见 使用说明.md。"""
import json,csv,re,os,sys,time,base64,urllib.request,argparse,concurrent.futures as cf
from collections import Counter,defaultdict
_ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
_ap.add_argument('list'); _ap.add_argument('outdir'); _ap.add_argument('legacy_outdir',nargs='?',help=argparse.SUPPRESS)
_ap.add_argument('--baseline',help='人工基线 CSV（可选，用于对比）')
_ap.add_argument('--mode',choices=['convertible','lenient','strict'],default='convertible')
_ap.add_argument('--runs',type=int,default=3,help='每条视觉调用次数；结果不一致→待确认')
_ap.add_argument('--optional-custom',choices=['keep','exclude'],default='keep',help='可选定制：keep=保留并在备注标注')
_ap.add_argument('--keyword',default='',help='本次列表的 EHunt 搜索词（写入输出）')
_ap.add_argument('--workers',type=int,default=8)
if __name__=='__main__': ARGS=_ap.parse_args()
else:  # 作为库被 worker 导入：用审定默认值
    ARGS=argparse.Namespace(list=None,outdir=None,legacy_outdir=None,baseline=None,mode='convertible',runs=3,optional_custom='keep',keyword='',workers=1)
if ARGS.legacy_outdir:  # 兼容旧用法: list baseline outdir
    ARGS.baseline,ARGS.outdir=ARGS.outdir,ARGS.legacy_outdir
LIST,OUT=ARGS.list,ARGS.outdir
MODEL='doubao-seed-2-1-turbo-260628'; URL='https://ark.cn-beijing.volces.com/api/v3/chat/completions'
PRICE_IN,PRICE_OUT=0.8,2.0  # 元/百万token，按需调整
def key():
    k=os.environ.get('ARK_API_KEY')
    if not k:
        for f in (os.path.join(os.path.dirname(os.path.abspath(__file__)),'..','..','worker','.env'),'/workspace/amazon/tools/etsy-analyzer/worker/.env'):
            if not os.path.exists(f): continue
            for l in open(f):
                if l.startswith('ARK_API_KEY='): k=l.split('=',1)[1].strip().strip('"\'')
            if k: break
    return k
KEY=key()
CUSTOM=r'\b(personali[sz]ed?|personalisation|personalization|custom(i[sz]ed)?|your (name|text|photo|logo)|with (name|photo|names)|name|names|photo(?!\s*(booths?|props?)\b)|monogram\w*|initials?|engraved|any text|logo)\b'
IP=r'\b(disney|marvel|pokemon|pok[eé]mon|star wars|harry potter|hogwarts|peter rabbit|hello kitty|sanrio|barbie|nfl|nba|mlb|taylor swift|swiftie|bluey|stitch|mickey|minnie|frozen|anime|naruto|one piece|sailor moon|ghibli|totoro|zelda|mario|nintendo|kpop|bts|labubu|snoopy|peanuts|grinch|sesame street|paw patrol|super ?bowl|eras tour)\b'
WHITELIST=r'(happy birthday|schulkind|mr\.? ?(&|and) ?mrs\.?|welcome|congrat\w*|merry christmas|happy (new year|easter|halloween|thanksgiving|holidays|hanukkah|valentine\w*)|season\'?s greetings|eid mubarak)'
OPTIONAL_HINT=r'(optional personali[sz]ation|personali[sz]ation (is )?optional|add (a )?names? \(optional\)|\(optional\)|optional (name|text|custom\w*)|with or without (name|personali[sz]ation))'
REQUIRED_HINT=r'(custom name|personali[sz]ed with your|enter (the |your )?names?|your name|add your)'
DIGITAL=r'\b(svg|stl|png|pdf|digital|download|printable|template|blank|blanks|laser cut file|cut file|bulk)\b'
def up(u):
    for s in ('il_794xN','il_570xN'):
        v=u.replace('il_120xN',s)
        try:
            r=urllib.request.urlopen(urllib.request.Request(v,headers={'User-Agent':'Mozilla/5.0'}),timeout=20)
            if r.status==200: return v,r.read()
        except Exception: pass
    return u,urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'Mozilla/5.0'}),timeout=20).read()
PROMPT='''你是Etsy选品审核员。目标：可批量生产、非定制、无IP的平面亚克力装饰品。根据图片和标题判断，只输出JSON：
{"分类":"排除|保留|待确认","原因":"定制/个性化|IP|材质不符|数字文件/素材/空白件|无","说明":"一句话","材质":"","是否平面亚克力":"是|可改造|不适合","定制类型":"无|可选定制|必须定制|待确认","核心卖点":["1-3项"],"适用场合":["1-3项，如婚礼/生日/圣诞/家居"]}
规则：需刻名字/文字/照片/日期/logo的→排除 定制/个性化；动漫游戏影视明星品牌球队同人→排除 IP；明显不是亚克力(木/金属/纸/布等)→原因填 材质不符，并判断“是否平面亚克力”：是=本身就是平面亚克力；可改造=设计/图案可以改做成平面亚克力(如海报、画框、带平面图案的木灯)；不适合=形态无法改成平面亚克力(如线香、穿戴甲)；素材/空白件/数字文件→排除。通用文字不算定制：Happy Birthday、Schulkind、Mr & Mrs、Welcome、Congrats、节日祝福等固定文字是批量款。效果图里出现的示例姓名/日期也不算定制，除非标题或描述写明 personalized/custom 可定制。定制类型：无=无需定制；可选定制=可加名字但不加也能直接卖(标题写 optional personalization/add name (optional) 等)，按保留处理；必须定制=下单必须提供名字/日期/照片(标题写 custom name/personalized with your/enter names 等)，只有这种才判 排除 定制/个性化；从列表页分不清是可选还是必须定制时填 待确认（不要默认可选定制）。拿不准一律“待确认”，绝不对存疑的排除。标题：'''
def vision(title,img):
    b={'model':MODEL,'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(img).decode()}},{'type':'text','text':PROMPT+title}]}],'response_format':{'type':'json_object'},'thinking':{'type':'disabled'},'temperature':0}
    for a in range(3):
        try:
            r=json.load(urllib.request.urlopen(urllib.request.Request(URL,json.dumps(b).encode(),{'Content-Type':'application/json','Authorization':'Bearer '+KEY}),timeout=120))
            t=r['choices'][0]['message']['content']; t=t[t.find('{'):t.rfind('}')+1]
            return json.loads(t),r.get('usage',{})
        except Exception as e: err=str(e)[:200]; time.sleep(2)
    return {'分类':'待确认','原因':'视觉调用失败','说明':err},{}

VALID=('定制/个性化','IP','材质不符','数字文件/素材/空白件')
def decide(j,title):
    """单次视觉结果 → (结果, 原因, 备注)。规则未命中才会走到这里。"""
    c=j.get('分类','待确认'); rs=j.get('原因','') or ''; fit=j.get('是否平面亚克力',''); ct=j.get('定制类型','无') or '无'; note=''
    if rs=='视觉调用失败': return '待确认','视觉调用失败',''
    if re.search(OPTIONAL_HINT,title,re.I) and not re.search(REQUIRED_HINT,title,re.I): ct='可选定制'
    if c=='排除' and not any(v in rs for v in VALID): c='待确认'
    if ct=='可选定制' and ARGS.optional_custom=='keep':
        note='可选定制'
        if '定制' in rs and '材质' not in rs: c,rs='保留',''
    if ct=='待确认' and (c!='排除' or '定制' in rs): c,note='待确认','定制类型待确认'
    if c=='排除' and '定制' in rs and '材质' not in rs: c='待确认'          # 视觉判定制但规则未命中 → 降级
    if '材质不符' in rs or (fit in('可改造','不适合') and c!='排除'):
        if ARGS.mode=='strict': c,rs='排除','材质不符'
        elif ARGS.mode=='lenient': c,rs='待确认','材质不符-'+(fit or '?')
        else: c,rs=(('排除','材质不符-形态不可改') if fit=='不适合' and '定制' not in rs else ('待确认','材质不符-可改造' if fit=='可改造' else '材质不符'))
    return c,(rs if c!='保留' else ''),note
MIN_SALES_7D=4  # 标准筛选：7 天销量 >3
def guard(o):
    """安全检查：EHunt 页面筛选万一失效，7 天销量 ≤3 的直接排除（不调视觉），记入审计。"""
    try: d7=int(o.get('sales_7d') or 0)
    except Exception: d7=0
    return ('排除','7天销量≤3',f'7天销量 {d7} < {MIN_SALES_7D}','无') if d7<MIN_SALES_7D else None
def rule(t):
    m=re.search(IP,t,re.I)
    if m: return '排除','IP','关键词:'+m.group(0),'无'
    m=re.search(CUSTOM,re.sub(WHITELIST,' ',t,flags=re.I),re.I)
    if m and not (ARGS.optional_custom=='keep' and re.search(OPTIONAL_HINT,t,re.I) and not re.search(REQUIRED_HINT,t,re.I)):
        return '排除','定制/个性化','关键词:'+m.group(0),'必须定制'
    m=re.search(DIGITAL,t,re.I)
    if m: return '排除','数字文件/素材/空白件','关键词:'+m.group(0),'无'
    return None
def classify_one(o,runs=None):
    """单条（已去重）→ 补全 结果/原因/备注/gate 等字段，返回 (o, usage Counter, 视觉调用次数)。worker 逐条调用。"""
    usage=Counter(); n=0; o.setdefault('image_url_hd',(o.get('image_url') or '').replace('il_120xN','il_794xN'))
    r=guard(o) or rule(o['title'])
    if r: o.update(结果=r[0],原因=r[1],说明=r[2],定制类型=r[3],来源='兜底' if r[1]=='7天销量≤3' else '规则',acrylic_fit='',备注='',votes='')
    else:
        u,img=up(o['image_url']); o['image_url_hd']=u; ds=[]
        for _ in range(runs or ARGS.runs):
            j,us=vision(o['title'],img); usage.update({k:v for k,v in us.items() if isinstance(v,int)}); n+=1; ds.append((decide(j,o['title']),j))
        finals=[d[0][0] for d in ds]; agree=len(set(finals))==1
        maj=Counter(finals).most_common(1)[0][0]; (c,rs,note),j=next(d for d in ds if d[0][0]==maj)
        if not agree: c,rs,note='待确认',rs or '多次结果不一致',(note+'；' if note else '')+'三次结果不一致'
        fit=Counter(d[1].get('是否平面亚克力','') for d in ds).most_common(1)[0][0]
        o.update(结果=c,原因=rs,备注=note,说明=j.get('说明',''),材质=j.get('材质',''),acrylic_fit=fit,定制类型=j.get('定制类型',''),来源='视觉',
                 核心卖点='; '.join(j.get('核心卖点',[]) or []),适用场合='; '.join(j.get('适用场合',[]) or []),votes='/'.join(finals),一致=agree)
    o['gate']={'保留':'passed','待确认':'pending','排除':'rejected'}[o['结果']]
    return o,usage,n
def reason_group(reason):
    """排除原因归类（只计数 + 本地日志用）。"""
    r=reason or ''
    return '7天销量≤3' if r=='7天销量≤3' else '定制' if '定制' in r else 'IP' if r=='IP' else '材质' if '材质' in r else '素材/数字文件' if any(k in r for k in ('数字','素材','空白')) else '其他'
def dedupe(rows):
    grp=defaultdict(list)
    for r in rows: grp[str(r['listing_id'])].append(r)
    out=[]
    for lid,g in grp.items():
        ps=[float(x['price']) for x in g]; o=dict(g[0]); o['listing_id']=lid; o['title']=o['title'].replace('\\/','/')
        o.update(ranks=[x['rank'] for x in g],price_min=min(ps),price_max=max(ps),total_sales=max(x['total_sales'] for x in g),sales_7d=max(x['sales_7d'] for x in g),dup_note='')
        o['price']=f"{min(ps):.2f}" if min(ps)==max(ps) else f"${min(ps):.2f}–{max(ps):.2f}"
        if len(g)>1: o['dup_note']=f"EHunt 返回{len(g)}条，疑似变体 (rank {'/'.join(map(str,o['ranks']))})" if min(ps)!=max(ps) else f"重复：rank {'/'.join(map(str,o['ranks']))} 已合并"
        o['sales_ratio']=round(o['sales_7d']/o['total_sales'],4) if o['total_sales'] else None
        out.append(o)
    return out
def run():
    t0=time.time(); rows=json.load(open(LIST)); os.makedirs(OUT,exist_ok=True)
    with open(OUT+'/raw_capture.csv','w',encoding='utf-8-sig',newline='') as fh:   # 原始抓取全量保留
        w=csv.DictWriter(fh,list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    items=dedupe(rows); todo=[]
    for o in items:
        o['image_url_hd']=o['image_url'].replace('il_120xN','il_794xN')
        r=guard(o) or rule(o['title'])
        if r: o.update(结果=r[0],原因=r[1],说明=r[2],定制类型=r[3],来源='兜底' if r[1]=='7天销量≤3' else '规则',acrylic_fit='',备注='',votes='')
        else: todo.append(o)
    usage=Counter(); calls=0
    def work(o):
        u,img=up(o['image_url']); o['image_url_hd']=u
        return o,[vision(o['title'],img) for _ in range(ARGS.runs)]
    with cf.ThreadPoolExecutor(ARGS.workers) as ex:
        for o,res in ex.map(work,todo):
            ds=[]; 
            for j,us in res:
                usage.update({k:v for k,v in us.items() if isinstance(v,int)}); calls+=1; ds.append((decide(j,o['title']),j))
            finals=[d[0][0] for d in ds]; agree=len(set(finals))==1
            maj=Counter(finals).most_common(1)[0][0]; pick=next(d for d in ds if d[0][0]==maj)
            (c,rs,note),j=pick
            if not agree: c,rs,note='待确认',rs or '多次结果不一致',(note+'；' if note else '')+'三次结果不一致'
            fits=Counter(d[1].get('是否平面亚克力','') for d in ds).most_common(1)[0][0]
            o.update(结果=c,原因=rs,备注=note,说明=j.get('说明',''),材质=j.get('材质',''),acrylic_fit=fits,定制类型=j.get('定制类型',''),来源='视觉',
                     核心卖点='; '.join(j.get('核心卖点',[]) or []),适用场合='; '.join(j.get('适用场合',[]) or []),votes='/'.join(finals),一致=agree)
    GATE={'保留':'passed','待确认':'pending','排除':'rejected'}
    for o in items: o['gate']=GATE[o['结果']]   # 预筛选初始闸门；待确认必须人工确认才进入选品筛选
    f=['listing_id','ranks','title','price','price_min','price_max','total_sales','sales_7d','sales_ratio','结果','gate','原因','备注','说明','来源','材质','acrylic_fit','定制类型','核心卖点','适用场合','votes','dup_note','image_url','image_url_hd']
    with open(OUT+'/selection_result.csv','w',encoding='utf-8-sig',newline='') as fh:
        w=csv.DictWriter(fh,f,extrasaction='ignore'); w.writeheader(); w.writerows(items)
    meta={'keyword':ARGS.keyword,'mode':ARGS.mode,'runs':ARGS.runs,'optional_custom':ARGS.optional_custom,'model':MODEL,'raw_rows':len(rows),'items':len(items),
          'vision_items':len(todo),'vision_calls':calls,'usage':dict(usage),'seconds':round(time.time()-t0,1),
          'cost_yuan':round(usage['prompt_tokens']/1e6*PRICE_IN+usage['completion_tokens']/1e6*PRICE_OUT,4),
          'counts':dict(Counter(o['结果'] for o in items)),'gate_counts':dict(Counter(o['gate'] for o in items)),'reasons':dict(Counter(o['原因'] for o in items if o['结果']!='保留')),
          'disagree_runs':sum(1 for o in todo if not o.get('一致',True))}
    json.dump({'meta':meta,'raw':rows,'items':items},open(OUT+'/selection.json','w'),ensure_ascii=False,indent=1,default=str)
    json.dump(meta,open(OUT+'/run_meta.json','w'),ensure_ascii=False,indent=1)
    print(json.dumps(meta,ensure_ascii=False))
if __name__=='__main__': run()
