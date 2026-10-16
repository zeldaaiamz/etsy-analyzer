# Etsy 选品自动筛选 · 使用说明

## 默认配置（2026-10-16 Hongyan 审定）
- 材质模式 `convertible`：明显非目标材质时，视觉判断能否改做平面亚克力。可改造 → 待确认；形态不可改（线香、穿戴甲等）→ 排除（材质不符-形态不可改）。
- `--optional-custom=keep`：可选定制（不加名字也能卖）保留，备注“可选定制”；只有“必须定制”才排除；分不清 → 待确认（备注“定制类型待确认”）。
- temperature=0，每条视觉跑 3 次；3 次结论不一致 → 待确认（备注“三次结果不一致”）。
- 拿不准就保留：规则没命中时，视觉的“定制/材质”排除一律降级为待确认（形态不可改除外）。

## 流程
1. 规则（只看标题）：IP 品牌词 → 排除 IP；定制词（personalized/custom/name/photo/monogram…，白名单 Happy Birthday/Schulkind/Mr & Mrs/Welcome/Congrats/节日祝福不算；photo booth/prop 不算）→ 排除 定制；svg/stl/digital/blank 等 → 排除 数字文件/素材/空白件。
2. 去重：同一 listing_id 合并为一行（价格不同显示区间、销量取较大值、备注“EHunt 返回N条，疑似变体”）；原始行全部保存在 raw_capture.csv。
3. 视觉（Doubao doubao-seed-2-1-turbo-260628，Ark）：缩略图自动升级为 il_794xN；返回 分类/原因/材质/是否平面亚克力/定制类型/核心卖点/适用场合。

## 用法
```bash
python3 replay_classify.py <EHunt列表.json> <输出目录> --keyword "acrylic decoration"
# 可选：--mode lenient|strict  --runs 1  --optional-custom exclude  --workers 8
# 兼容旧用法：replay_classify.py list.json baseline.csv outdir
```
输入 JSON：EHunt 列表页抓取的数组 `[{rank,title,price,total_sales,sales_7d,listing_id,image_url}]`。
密钥：环境变量 ARK_API_KEY，未设置时读 etsy-analyzer/worker/.env（不会打印）。

## 输出
- `selection_result.csv`（UTF-8 BOM）：每个 listing 一行，含 结果/原因/备注/acrylic_fit/定制类型/核心卖点/适用场合/三次投票/7天÷总销量/794 图。
- `selection.json`：meta + raw + items，供选品雷达导入（etsy-analyzer 仓库 tools/etsy_selection/import_run.py）。
- `raw_capture.csv`：原始抓取全量；`run_meta.json`：计数、tokens、费用、耗时。

## 参考（acrylic decoration 回放，96 个 listing）
保留 24 / 待确认 8 / 排除 64；视觉 111 次，约 20.7 万 tokens，约 ¥0.18，约 5 分钟（8 并发）。
旧版回放脚本：replay_classify_v2_legacy.py + replay_aggregate.py。

## 线上流程（按需，2026-10-16 定稿）
1. 「选品预筛」#/selscreen 输入关键词 → **查询数量**：`sel_count()` 建轻量任务 → worker 在 EHunt 界面设好筛选后只读 “Find N products” → 页面显示 N 条（acrylic decoration = 505）。
   - N ≤ 100 全读；N > 100 默认前 100 可调；N > 1000 建议跳过等人工决定（`sel_skip`）。查到数量前“开始抓取”按钮禁用。
2. 填最多读取条数 → **开始抓取**：`sel_start(run, limit)`。
3. worker 逐行读列表（每页 20 条），每行：原始行入 `sel_raw_rows` → 同 listing 合并 → 标题关键词 + 视觉×3 + 陈选六条规则分类。
   - **排除的不进入预筛**，只进页面上的审计清单（“已排除 64：定制 55 / IP 4 / 材质 4 …”，可展开、可恢复），满足规则 6。
   - 保留、待确认进入预筛；拿不准一律待确认。
4. 预筛（待确认 / 保留 / 全部）由 Hongyan 人工复核：✓ 保留、✕ 排除（进审计清单）。只有“保留”的进入「选品筛选」#/select。
`import_run.py` 只用于离线回放导入。

## EHunt 界面操作（筛选不在 URL 里，必须点界面）
页面 https://ehunt.ai/etsy-product-research：顶部搜索框（placeholder “Enter a product name, tag, product ID...”）输入关键词 → 橙色 Search；
Basic: 行 Price(USD) 下拉自定义 Min 8 / Max 40 → OK；Sales 下拉 “Total sales:” Min 100 → OK；结果右上排序（Export to CSV 旁）默认 “↓7-day sales”，校验；
校验 “Selected:” 有 “Price: 8~40” 和 “Total sales: 100~”；数量用 `/Find ([\d,]+) products/`。任一校验失败 → 任务失败并截图到日志目录，不会用未筛选的数（如 43,805）。
选择器都在 `UI_DEFAULTS`，可用环境变量 `EHUNT_UI`（JSON）覆盖；筛选值可用 `EHUNT_FILTERS` 覆盖。当前选择器是占位，首次真实运行前需对照页面校准。

## 运行位置：本 box（不是 ECS）
- 原 ECS systemd 设计（etsy-selection-worker.service）已作废并删除。
- `deploy/box-selection-worker.sh login|start|stop|status|once-fake <json>`；进程 nohup 常驻轮询 Supabase；分类（Doubao，ARK_API_KEY）也在 box 上跑。
- 密钥放 `~/.config/etsy-selection/worker.env`（600），脚本不打印。
- 浏览器用**自己的** Playwright 持久化目录 `~/.local/share/etsy-selection/ehunt-profile`，不连接、不复制桌面浏览器的 cookie/profile。
- 首次：`deploy/box-selection-worker.sh login` 在 box 显示器（DISPLAY=:1）打开有头浏览器到 EHunt 页面，**人工登录**后关闭窗口即保存；脚本不会自动填写账号。
- 测试：`deploy/box-selection-worker.sh once-fake 列表.json`（不开浏览器）。

## 风险
- EHunt 条款可能不允许自动化读取页面；账号风控。保持低频（每行 0.3 秒、翻页 2 秒、单任务串行）。
- 登录过期 / 二次验证 / 验证码：任务失败并提示重新 `login`。
- 界面改版使选择器失效；每次都校验 Selected: 标签和排序，避免拿到未筛选数据。
- box 重启后需 `start`（可改 systemd --user）；box 与桌面共享机器，浏览器内存约 400–800M。
- 成本：约 ¥0.18 / 100 行（视觉×3），约 5 分钟 / 100 行。
