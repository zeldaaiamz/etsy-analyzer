#!/usr/bin/env python3
"""L1：给 index.html 里的 css/app.css、js/app.js 加内容哈希查询串（?v=xxxxxxxx），发布后浏览器立即拿到新版本。
用法：python3 tools/version_assets.py   （改了 app.js / app.css 后运行一次）"""
import hashlib, pathlib, re
web = pathlib.Path(__file__).resolve().parent.parent / "web"
html = (web / "index.html").read_text()
for rel in ("css/app.css", "js/app.js"):
    h = hashlib.sha1((web / rel).read_bytes()).hexdigest()[:8]
    html = re.sub(re.escape(rel) + r'(\?v=[0-9a-f]+)?"', f'{rel}?v={h}"', html)
(web / "index.html").write_text(html)
print("versioned", re.findall(r'(?:css/app\.css|js/app\.js)\?v=[0-9a-f]+', html))
