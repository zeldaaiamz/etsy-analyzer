#!/usr/bin/env python3
"""L1：给 index.html 里的 css/app.css、js/app.js 加内容哈希查询串（?v=xxxxxxxx），发布后浏览器立即拿到新版本。
用法：python3 tools/version_assets.py   （改了 app.js / app.css 后运行一次）"""
import hashlib, pathlib, re
web = pathlib.Path(__file__).resolve().parent.parent / "web"
html = (web / "index.html").read_text()
sel = web / "js" / "selscreen.js"   # selscreen.js 自己 import 的 selqueue.js：先打哈希进 selscreen.js
sel.write_text(re.sub(r'"\./selqueue\.js(\?v=[0-9a-f]+)?"', f'"./selqueue.js?v={hashlib.sha1((web / "js" / "selqueue.js").read_bytes()).hexdigest()[:8]}"', sel.read_text()))
bn = hashlib.sha1((web / "js" / "batchname.js").read_bytes()).hexdigest()[:8]   # screen.js / selscreen.js import 的 batchname.js
for f in (web / "js" / "screen.js", sel):
    f.write_text(re.sub(r'"\./batchname\.js(\?v=[0-9a-f]+)?"', f'"./batchname.js?v={bn}"', f.read_text()))
app = web / "js" / "app.js"  # 子模块（screen.js）先打哈希进 app.js，再算 app.js 自己的哈希
src = app.read_text()
for mod in ("screen", "selscreen", "exwords", "reconcile"):   # app.js 直接 import 的子模块：改了任何一个都要换哈希，否则浏览器用旧缓存
    h = hashlib.sha1((web / "js" / f"{mod}.js").read_bytes()).hexdigest()[:8]
    src = re.sub(rf'"\./{mod}\.js(\?v=[0-9a-f]+)?"', f'"./{mod}.js?v={h}"', src)
app.write_text(src)
for rel in ("css/app.css", "js/app.js"):
    h = hashlib.sha1((web / rel).read_bytes()).hexdigest()[:8]
    html = re.sub(re.escape(rel) + r'(\?v=[0-9a-f]+)?"', f'{rel}?v={h}"', html)
(web / "index.html").write_text(html)
print("versioned", re.findall(r'(?:css/app\.css|js/app\.js)\?v=[0-9a-f]+', html))
