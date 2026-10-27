// 顶栏左上角「切换」下拉：跳到其他工具（新标签页打开）。点击开关，Esc / 点外面 / 选中后关闭；↑↓ 在项间移动。
const wrap = document.getElementById("siteSw");
const btn = document.getElementById("siteSwBtn");
const pop = document.getElementById("siteSwPop");
if (wrap && btn && pop) {
  const items = () => [...pop.querySelectorAll(".sitesw-i")];
  const isOpen = () => !pop.classList.contains("hidden");
  const setOpen = (on, focusFirst = false) => {
    pop.classList.toggle("hidden", !on);
    btn.setAttribute("aria-expanded", String(on));
    wrap.classList.toggle("open", on);
    if (on && focusFirst) items()[0]?.focus();
  };
  btn.addEventListener("click", () => setOpen(!isOpen()));
  btn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true, true); }
  });
  pop.addEventListener("keydown", (e) => {
    const list = items(), i = list.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[(i - 1 + list.length) % list.length]?.focus(); }
  });
  pop.addEventListener("click", (e) => { if (e.target.closest(".sitesw-i")) setOpen(false); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isOpen()) { setOpen(false); btn.focus(); }
  });
  document.addEventListener("click", (e) => { if (isOpen() && !wrap.contains(e.target)) setOpen(false); });
  wrap.addEventListener("focusout", (e) => { if (isOpen() && e.relatedTarget && !wrap.contains(e.relatedTarget)) setOpen(false); });
}
