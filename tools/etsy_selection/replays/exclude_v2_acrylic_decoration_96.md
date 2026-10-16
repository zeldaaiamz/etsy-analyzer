# 排除词 v2 回放：acrylic decoration 96 条（旧规则 vs 新规则）

- 数据：`/workspace/amazon/选品/replay/replay_result_v2.csv`（acrylic decoration EHunt 100 行去重后 96 条）
- 只比较标题规则这一步（7 天销量兜底 + IP + 定制 + 素材词），不重新调用豆包。
- 新规则：定制类加 address / return address（整词）；不加 adjust/adjustable；不新增素材类词；去掉 bulk；wholesale / vintage 不排除。

| | 旧规则 | 新规则 |
|---|---|---|
| 规则直接排除 | 89 | 89 |
| 新增排除 | | 0 |
| 不再排除 | | 0 |

## 新增排除（请抽查）

无：96 条里没有标题含 address / return address 的条目。

## 不再被规则排除（之后会走豆包判断）

无。

## 规则自检（合成标题，非真实数据）

- Return Address Stamp Acrylic Block → 排除，命中 Return Address
- Acrylic House Number Address Sign → 排除，命中 Address
- Bulk Acrylic Ornaments Wholesale Lot → 不排除
- Vintage Style Acrylic Sign → 不排除
- Adjustable Acrylic Phone Stand → 不排除
