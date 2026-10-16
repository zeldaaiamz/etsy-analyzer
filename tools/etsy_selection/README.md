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
