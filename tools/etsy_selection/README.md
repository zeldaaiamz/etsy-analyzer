# Etsy 获取链接（选品雷达模块）

代码：`classify.py`（规则 + 豆包分类，默认 convertible + optional-custom=keep）、`../../worker/selection/ehunt_worker.py`（box 抓取进程）、`lookup.py`（序猿查询）、`gen_demo.py`（预览数据）、迁移 `supabase/migrations/024_etsy_selection.sql`（追加，未上线）。

## 获取链接 · 定稿（Hongyan 2026-10-16，覆盖之前所有版本）

1. **页面**：「获取链接」（#/selscreen），导航在「任务」前面。网页**没有搜索**；查历史/查某条链接请在对话里问序猿（`tools/etsy_selection/lookup.py`）。
2. **先查数量**：输入关键词 → 「查询数量」→ box 上的抓取进程在 EHunt 列表页设好筛选，只读结果数（“Find N products”）。筛选与抓取完全一致：
   价格 $8–40 · 总销量 ≥100 · 7 天销量 >3（Sales 下拉 “7-day sales” Min 4，含 4）· 按 7 天销量降序。条件 chip：Keyword / `Price: 8~40` / `7-day sales: 4~` / `Total sales: 100~`。
   实测（10-16）：acrylic decoration = **20**（“Find 20 products”）；sign = **1,688**（85 页）。之前记的 acrylic decoration 505 是**没加 7 天销量筛选**时的数，已作废。未实测的数在预览里标「演示数字」。
3. **抓取**：显示数量后才能填「最多读取 N 条」并点「抓取」。抓取在 **本 box** 上运行（`deploy/box-selection-worker.sh`），不在阿里云 ECS。
4. **规则先筛**：标题规则（定制/个性化、IP、数字文件/素材/空白件）+ 7 天销量 ≤3 兜底。命中即排除：**不调豆包、不显示、不入库**；页面只有一行汇总，如“已排除 13：定制 11、IP 1、素材/数字文件 1”。
5. **豆包判断**：规则通过的才送豆包视觉（convertible + optional-custom=keep，temperature 0，3 次，不一致→待确认）。结果页显示「符合要求 N 条」+ 纯 Etsy 链接清单（`https://www.etsy.com/listing/<id>`，每行一条，无标签/字段），可「复制本批」「复制全部批次」（一键复制纯链接清单，无 txt/csv 模式选择）、下载 TXT（无 CSV）。待确认也算符合要求（拿不准就保留）。
6. **分批**：超过 1000 条按每批 1000 读（第 1 批 = 排名 1–1000 = 第 1–50 页）。每批完成后才更新一次「还剩 N 条未读」，并给出「读取下一批（1001–2000）」和「一键读取全部剩余」（需确认）。EHunt 最多只显示前 5,000 条 → 最多 5 批 / 5,000 条。跨批按 listing_id 去重；worker 按批次的 offset/page 续读。已复制的批次自动折叠。
7. **入库**：DB 只存符合要求的链接（sel_items，verdict 只能是 保留/待确认）。被排除的链接不入库；run/batch 只记汇总数（已读/符合/排除及各原因计数）。
8. **保留期**：DB 里的数据按批次完成时间保留 3 天，之后自动删除（items + batches + runs 一起），不归档。`sel_purge_older_than(interval '3 days')`；pg_cron 可用时每日执行，否则由 box 抓取进程启动时和每 24 小时执行。
9. **查询**：网页不提供搜索；序猿用 `lookup.py runs | links <run> | listing <id> | excluded <run>` 在对话里回答。
10. **排除原因（已确认）**：写在 box 本地 `~/.local/share/etsy-selection/exclusion-logs/<run_id>.jsonl`（时间、批次、排名、listing、标题、原因、依据、来源 规则/兜底/视觉），3 天后自动删除，供抽查。

**首次抓取前**：需要 Hongyan 通过安全表单完成一次 EHunt 登录（`deploy/box-selection-worker.sh login` 在 box 显示器上打开有头浏览器，登录状态保存在 box 专用 profile `~/.local/share/etsy-selection/ehunt-profile`，不使用、不复制桌面浏览器的 cookie）。序猿不代为输入账号密码。

**省豆包调用（回放实测）**：acrylic decoration 加全部筛选后的 20 条里，13 条被标题规则直接排除，只有 7 条送豆包 → 视觉调用从 60 次降到 21 次（3 次投票），省 65%。

## 代码顺序（省豆包）
`ehunt_worker.process_batch`：去重（跨批）→ `classify.guard`（7 天销量）→ `classify.rule`（标题）→ 命中：计数 + 本地日志，结束 → 未命中：`classify.classify_one`（豆包 ×3）→ 排除：计数 + 本地日志；保留/待确认：写 `sel_items`。
