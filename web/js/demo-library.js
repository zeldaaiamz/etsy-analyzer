// ?demo=1：产品库示例（三个阶段都有；含关联任务的条目、任务已删除仅保留快照的条目）
const svg = (bg, fg, label) => "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400"><rect width="400" height="400" fill="${bg}"/><circle cx="200" cy="170" r="90" fill="${fg}" opacity=".85"/><text x="200" y="330" font-size="34" font-family="sans-serif" fill="#fff" text-anchor="middle">${label}</text></svg>`)));
const EXTRA = [
  ["Handmade Ceramic Ring Dish – Personalized Initial Jewelry Tray, Bridesmaid Gift", 18.5, ["ring dish personalized", "bridesmaid gift", "jewelry tray"], 2, 6, "candidate", [], null, "#5b4a3a", "#e8c9a0", "Ring Dish"],
  ["Custom Pet Portrait Embroidered Sweatshirt – Dog Mom Gift", 42, ["pet portrait sweatshirt", "dog mom gift"], 0, 11, "candidate", [], null, "#2f4858", "#86bbd8", "Pet Sweat"],
  ["Minimalist Birth Flower Necklace, 14k Gold Filled", 29.99, ["birth flower necklace", "gold filled necklace"], 5, 9, "active", [], null, "#4a3b52", "#f2c14e", "Necklace"],
  ["Personalized Leather Dog Collar with Brass Name Plate", 34, ["leather dog collar", "personalized collar"], 1, 4, "active", [], null, "#3d2c23", "#c08552", "Collar"],
  ["Halloween Ghost Candle Set of 3 – Soy Wax", 24, ["ghost candle", "halloween decor"], 3, 14, "rejected", ["seasonal", "logistics"], "蜡烛运输易变形，旺季只有 6 周", "#22223b", "#f2e9e4", "Ghost"],
  ["Disney Inspired Castle Wall Art Print", 12, ["castle wall art", "nursery print"], 7, 22, "rejected", ["ip_risk", "competition", "other"], "IP 相似度高，平台下架风险", "#1d3557", "#a8dadc", "Wall Art"],
  ["Large Macrame Wall Hanging 120cm Boho Decor", 89, ["macrame wall hanging"], 0, 3, "rejected", ["cost_high", "roi_low"], null, "#6b4f3a", "#efe1c6", "Macrame"],
];
export const libraryFixtures = (fx) => {
  const ok = fx.jobs.filter((j) => j.status === "succeeded"), e = new Map(fx.etsy.map((r) => [r.job_id, r])), a = new Map(fx.analyses.map((r) => [r.job_id, r]));
  const day = (d) => new Date(new Date(fx.now).getTime() - d * 864e5).toISOString();
  const items = ok.slice(0, 2).map((j, i) => { const p = e.get(j.id) || {}, an = a.get(j.id) || {};
    return { id: `demo-lib-${i}`, job_id: j.id, etsy_listing_id: j.etsy_listing_id || p.listing_id, etsy_url: j.etsy_url, title: p.title, image_url: p.image_url,
      price: p.price, product_summary: an.product_summary, target_audience: an.target_audience, keywords: an.keywords, identical_count: 2 + i, similar_count: 8 - i,
      stage: i ? "active" : "candidate", reject_reasons: [], stage_note: null, added_at: day(i), stage_changed_at: i ? day(0) : null }; });
  EXTRA.forEach(([title, price, keywords, ic, sc, stage, reasons, note, bg, fg, lbl], i) => items.push({ id: `demo-lib-x${i}`, job_id: null, etsy_listing_id: 1488800010 + i,
    etsy_url: `https://www.etsy.com/listing/${1488800010 + i}`, title, image_url: svg(bg, fg, lbl), price, keywords, identical_count: ic, similar_count: sc,
    stage, reject_reasons: reasons, stage_note: note, added_at: day(i + 1), stage_changed_at: stage === "candidate" ? null : day(i * 0.5) }));
  return items;
};
