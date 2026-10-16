# 排除词 v2 回放：acrylic decoration 96 条（旧规则 vs 新规则）

- 数据：`/workspace/amazon/选品/replay/replay_result_v2.csv`（acrylic decoration EHunt 100 行去重后 96 条）
- 只比较标题规则这一步（7 天销量兜底 + IP + 定制 + 素材词），不重新调用豆包。
- 新规则：定制类加 address / return address（整词）；不加 adjust/adjustable；素材/数字文件/非成品类整组取消（svg/stl/png/pdf/digital/download/printable/template/blank(s)/cut file 等不再排除）；wholesale / bulk / vintage 不排除。
- 标题规则不再排除的条目会改走豆包视觉判断；豆包若仍判“素材/数字文件”，现在降为待确认，不排除。

| | 旧规则 | 新规则 |
|---|---|---|
| 规则直接排除 | 89 | 88 |
| 新增排除 | | 0 |
| 不再排除 | | 1 |

## 新增排除（请抽查）

无：96 条里没有标题含 address / return address 的条目。

## 新通过标题规则的条目（之前被规则排除，现在改走豆包判断；请抽查）

| # | 标题 | 链接 | 之前命中的词 | 之前原因 |
|---|---|---|---|---|
| 1 | STL + 3MF 3D printing incl. commercial license - Fizzelbart - Elf - Gnome - Luminous - Beard - Balloon light - Decoration - LED - Bundle | https://www.etsy.com/listing/4342102475 | STL | 数字文件/素材/空白件 |

## 之前由豆包判“素材/数字文件”排除的条目（现在会降为待确认）

无。

## 规则自检（合成标题，非真实数据）

- Acrylic SVG Cut File → 不排除
- Printable Digital Download Sign → 不排除
- Blank Acrylic Keychain Blanks → 不排除
- Return Address Stamp Acrylic Block → 排除，命中 Return Address
- Acrylic House Number Address Sign → 排除，命中 Address
- Bulk Acrylic Ornaments Wholesale Lot → 不排除
- Vintage Style Acrylic Sign → 不排除
- Adjustable Acrylic Phone Stand → 不排除
