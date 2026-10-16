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

## 线上流程（按需抓取，2026-10-16 起）
1. 用户在选品雷达「选品预筛」#/selscreen 输入关键词 → 点“开始抓取并预筛” → `sel_start()` 在 `sel_runs` 建任务（queued；每人同时最多 1 个）。
2. ECS 上的 `etsy-selection-worker`（systemd，与 etsy-analyzer-worker 同模式、独立进程，见 deploy/etsy-selection-worker.service）用 `sel_claim_run()` 认领（FOR UPDATE SKIP LOCKED，心跳超时 10 分钟回收）。
3. worker（worker/selection/ehunt_worker.py）用 Playwright 持久化登录的 Chromium 打开 EHunt 列表页（价格 $8–40、总销量 ≥100、按 7 天销量降序），**逐行读 DOM**（不用 EHunt API），每行：写 `sel_raw_rows` → upsert `sel_items`（item_state=scraped，同 listing 合并）→ 立即 `classify.classify_one()`（规则 + 视觉×3）→ item_state=classified + 初始 gate。
4. 前端每 3 秒轮询（024 也把两张表加进 Realtime，可改订阅），条目逐条出现，带进度条；人工复核 ✓/✕/恢复（`sel_set_gate`）。
5. 只有 gate=passed 的进入「选品筛选」#/select（短名单 / 未全勾 / 机器保留 / 人工确认 / 全部已通过）→ 加入产品库备选。
`import_run.py` 只用于离线回放导入。

## 抓取方案与会话
- Playwright `launch_persistent_context(EHUNT_PROFILE_DIR)`，headless Chromium；首次在服务器上用有头模式/VNC 人工登录 EHunt 一次，cookie 留在 profile 目录（权限 700，仅 worker 用户可读，不进 git）。
- 列表页 URL 模板 `EHUNT_LIST_URL`（含 {kw}），DOM 选择器 `EHUNT_SELECTORS`（JSON）都放 .env，便于 EHunt 改版时只改配置。
- 每行间隔 0.3 秒、翻页 2 秒，单 worker 单任务串行；检测到跳到登录页 → 任务 failed「EHunt 登录已失效」，前端显示，需人工重新登录。
- 测试：`python -m selection.ehunt_worker --fake 列表.json --once` 不开浏览器。

## 风险
- EHunt 服务条款可能不允许自动化读取页面；账号可能被风控或封禁。上线前需确认，或保持低频。
- 登录态过期 / 二次验证 / 验证码：headless 下无法自动处理，只能报错等人工。
- DOM 改版导致选择器失效：上线前须对照真实页面校准（当前选择器是占位）。
- ECS 内存：Chromium 需 400–800M，单独 unit（MemoryMax=1200M），不能并进 analyzer（500M 上限）。
- 视觉成本：约 ¥0.18 / 100 行（3 次投票），时长约 5 分钟 / 100 行。
