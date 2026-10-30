// node --test web/tests/ —— 顶栏左上角「切换」下拉（关键词工作台 / VOC / sif关键词选品）
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const js = readFileSync(new URL("../js/siteswitch.js", import.meta.url), "utf8");

test("顶栏下拉恰好三项，新标签页打开且 rel=noopener", () => {
  const pop = html.match(/<div id="siteSwPop"[\s\S]*?<\/div>/)[0];
  const links = [...pop.matchAll(/<a [^>]*>/g)].map((m) => m[0]);
  assert.equal(links.length, 3);
  assert.match(links[0], /href="https:\/\/zeldaaiamz\.github\.io\/zeldaaiamz1\/tool\/\?v=polish-20261014-2"/);
  assert.match(links[1], /href="https:\/\/zeldaaiamz\.github\.io\/zeldaaiamz1\/voc\/"/);
  assert.match(links[2], /href="https:\/\/zeldaaiamz\.github\.io\/zeldaaiamz1\/root-finder\/"/);
  for (const a of links) { assert.match(a, /target="_blank"/); assert.match(a, /rel="noopener"/); }
  assert.match(pop, /关键词工作台/); assert.match(pop, /VOC/); assert.match(pop, /sif关键词选品/);
  assert.ok(html.indexOf('id="siteSw"') < html.indexOf('id="nav"'), "位于顶栏左侧品牌区");
  assert.match(html, /js\/siteswitch\.js\?v=[0-9a-f]{8}/);
});

test("Esc / 点外面关闭", () => {
  assert.match(js, /"Escape"/);
  assert.match(js, /wrap\.contains\(e\.target\)/);
});
