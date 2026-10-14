// ?demo=1：产品库示例（一条关联现有任务，一条任务已删除仅保留快照）
export const libraryFixtures = (fx) => {
  const ok = fx.jobs.filter((j) => j.status === "succeeded"), e = new Map(fx.etsy.map((r) => [r.job_id, r])), a = new Map(fx.analyses.map((r) => [r.job_id, r]));
  const items = ok.slice(0, 1).map((j, i) => { const p = e.get(j.id) || {}, an = a.get(j.id) || {};
    return { id: `demo-lib-${i}`, job_id: j.id, etsy_listing_id: j.etsy_listing_id || p.listing_id, etsy_url: j.etsy_url, title: p.title, image_url: p.image_url,
      price: p.price, product_summary: an.product_summary, target_audience: an.target_audience, keywords: an.keywords, note: null, added_at: fx.now }; });
  items.push({ id: "demo-lib-orphan", job_id: null, etsy_listing_id: 1488800012, etsy_url: "https://www.etsy.com/listing/1488800012/ceramic-ring-dish",
    title: "Handmade Ceramic Ring Dish – Personalized Initial", image_url: null, price: 18.5, product_summary: "个性化陶瓷首饰托盘",
    target_audience: "送礼人群（伴娘/闺蜜）", keywords: ["ring dish personalized"], note: null, added_at: new Date(new Date(fx.now).getTime() - 864e5).toISOString() });
  return items;
};
