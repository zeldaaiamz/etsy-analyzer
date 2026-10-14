import { createClient } from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm";
import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./config.js";

const sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LISTING_RE = /^https?:\/\/(www\.)?etsy\.com\/([a-z]{2}(-[a-z]{2})?\/)?listing\/\d+/i;

$("btnLogin").onclick = async () => {
  const { error } = await sb.auth.signInWithOtp({ email: $("email").value, options: { emailRedirectTo: location.href } });
  $("loginMsg").textContent = error ? error.message : "已发送登录链接，请查收邮件";
};
$("btnLogout").onclick = () => sb.auth.signOut();

sb.auth.onAuthStateChange((_e, session) => {
  $("login").classList.toggle("hidden", !!session);
  $("app").classList.toggle("hidden", !session);
  if (session) { $("who").textContent = session.user.email; loadAll(); subscribe(); }
});

$("btnSubmit").onclick = async () => {
  const urls = [...new Set($("urls").value.split(/\s+/).map((s) => s.trim()).filter(Boolean))];
  const ok = urls.filter((u) => LISTING_RE.test(u)), bad = urls.length - ok.length;
  if (!ok.length) return ($("submitMsg").textContent = "没有有效的 Etsy listing 链接");
  const { data: batch, error } = await sb.from("batches").insert({ name: new Date().toISOString() }).select().single();
  if (error) return ($("submitMsg").textContent = error.message);
  const { error: e2 } = await sb.from("jobs").insert(ok.map((u) => ({ batch_id: batch.id, etsy_url: u })));
  $("submitMsg").textContent = e2 ? e2.message : `已提交 ${ok.length} 条${bad ? `，忽略无效 ${bad} 条` : ""}`;
  $("urls").value = ""; loadAll();
};

let state = { jobs: new Map(), analyses: new Map(), matches: new Map() };

async function loadAll() {
  const [j, a, m] = await Promise.all([
    sb.from("jobs").select("*").order("created_at", { ascending: false }).limit(200),
    sb.from("analyses").select("*"),
    sb.from("amazon_matches").select("job_id,asin,title,url,review_count,bsr,est_monthly_sales,price,similarity").order("similarity", { ascending: false }),
  ]);
  state.jobs = new Map((j.data || []).map((r) => [r.id, r]));
  state.analyses = new Map((a.data || []).map((r) => [r.job_id, r]));
  state.matches = new Map();
  (m.data || []).forEach((r) => { if (!state.matches.has(r.job_id)) state.matches.set(r.job_id, []); state.matches.get(r.job_id).push(r); });
  render();
}

let channel;
function subscribe() {
  if (channel) return;
  // RLS 同样作用于 Realtime：只会收到自己的行
  channel = sb.channel("rt")
    .on("postgres_changes", { event: "*", schema: "public", table: "jobs" }, (p) => { state.jobs.set(p.new.id, p.new); render(); })
    .on("postgres_changes", { event: "*", schema: "public", table: "analyses" }, (p) => { state.analyses.set(p.new.job_id, p.new); render(); })
    .on("postgres_changes", { event: "INSERT", schema: "public", table: "amazon_matches" }, () => loadAll())
    .subscribe();
}

function render() {
  const jobs = [...state.jobs.values()].sort((x, y) => y.created_at.localeCompare(x.created_at));
  $("rows").innerHTML = jobs.map((j) => {
    const a = state.analyses.get(j.id) || {};
    const ms = (state.matches.get(j.id) || []).slice(0, 5).map((m) =>
      `<div><a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.asin)}</a> ${esc((m.title || "").slice(0, 40))} · ⭐${esc(m.review_count)} · BSR ${esc(m.bsr ?? "-")} · ~${esc(m.est_monthly_sales ?? "-")}/月</div>`).join("");
    const st = j.status === "running" ? `running (${esc(j.stage)})` : j.status;
    return `<tr><td class="${esc(j.status)}">${esc(st)}${j.error ? `<br><small>${esc(j.error)}</small>` : ""}</td>
      <td><a href="${esc(j.etsy_url)}" target="_blank" rel="noopener">${esc(j.etsy_listing_id || "链接")}</a></td>
      <td>${esc(a.product_summary)}<br><small>${esc(a.category)}</small></td><td>${esc(a.target_audience)}</td>
      <td>${esc((a.keywords || []).join(", "))}</td><td>${esc(a.competition_level)}</td><td>${esc(a.demand_level)}</td>
      <td>${esc(a.opportunity_score)}</td><td>${esc(a.verdict)}</td><td>${ms}</td></tr>`;
  }).join("");
}
