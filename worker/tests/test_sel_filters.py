"""EHunt 筛选（价格 / 总销量 / 7 天销量）施加与校验：2026-11-17 volufiline 等「筛选未生效」修复。

根因：_open_list 打开下拉后固定 sleep(1) 就填 Min/Max、点 OK，不确认面板已就绪、值已写进、chip 已出现 →
偶发丢掉某一个筛选（Price 缺失 / 只剩 "Price: <40" / 全部缺失）。这里用假页面模拟这些竞态。"""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402

U = W.ui()
F = W.filters({})
PRICE_CHIP, TOTAL_CHIP, D7_CHIP = "Price: 8~40", "Total sales: 100~", "7-day sales: 4~"


# ---------- 纯函数：chip 校验 ----------
def test_missing_chips_all_present_with_x_separators():
    sel = "Selected:\nKeyword: cup\n×\nPrice: 8~40\n×\n7-day sales: 4~\n×\nTotal sales: 100~\n×\n×\nSaved filters"
    assert W.missing_chips(sel, ["Keyword: cup", PRICE_CHIP, TOTAL_CHIP, D7_CHIP]) == []


def test_missing_chips_partial_price_is_missing():
    sel = "Selected:\nKeyword: heater\n×\nPrice: <40\n×\n7-day sales: 4~\n×\nTotal sales: 100~\n×\n×\nSaved filters"
    assert W.missing_chips(sel, [PRICE_CHIP, TOTAL_CHIP]) == [PRICE_CHIP]


def test_missing_chips_keyword_only():
    sel = "Selected:\nKeyword: volufiline\n×\n×\nSaved filters"
    assert W.missing_chips(sel, [PRICE_CHIP, TOTAL_CHIP, D7_CHIP]) == [PRICE_CHIP, TOTAL_CHIP, D7_CHIP]


def test_missing_chips_ignores_spaces_and_case():
    assert W.missing_chips("price:8 ~ 40", [PRICE_CHIP]) == []


def test_filter_not_applied_is_retry_kind_and_runtime_error():
    assert issubclass(W.FilterNotApplied, RuntimeError)
    assert W.FilterNotApplied in W.RETRY_KINDS and W.RETRY_KINDS[W.FilterNotApplied] == "筛选未生效"
    assert not W.is_poisoned(W.FilterNotApplied("筛选未生效"))


# ---------- 假 EHunt 页面 ----------
class FakeList:
    def __init__(self, items): self.items = list(items)
    @property
    def first(self): return self.items[0] if self.items else Missing()
    def nth(self, i): return self.items[i]
    def count(self): return len(self.items)
    def is_visible(self): return bool(self.items) and self.items[0].is_visible()
    def locator(self, sel): return FakeList([x for it in self.items for x in it.locator(sel).items])
    def filter(self, has=None, has_not=None, visible=None):   # 同 Playwright Locator.filter(has=, has_not=, visible=)
        vis = lambda p, sel: any(x.is_visible() for x in p.locator(sel).items)
        return FakeList([p for p in self.items if (visible is None or p.is_visible() == visible)
                         and (has is None or vis(p, has.sel)) and (has_not is None or not vis(p, has_not.sel))])


class Sel(FakeList):
    """pg.locator(内部选择器) —— 只用作 filter(has=...) 的参数。"""
    def __init__(self, sel): super().__init__([]); self.sel = sel


class Missing:
    def is_visible(self): return False
    def count(self): return 0
    def click(self, timeout=None): raise AssertionError("clicked a missing element")
    def locator(self, sel): return FakeList([])


class Input:
    def __init__(self, page): self.page, self.value, self.selected = page, "", False
    def is_visible(self): return True
    def count(self): return 1
    @property
    def first(self): return self
    def click(self, timeout=None): self.page.events.append("input_click")
    def press(self, key, timeout=None):
        if key in ("Control+A", "Meta+A"): self.selected = True
        elif key in ("Backspace", "Delete") and self.selected: self.value, self.selected = "", False
        elif key in ("Backspace",): self.value = self.value[:-1]
    def press_sequentially(self, text, delay=None, timeout=None):
        if self.page.lose_typing > 0: self.page.lose_typing -= 1; return     # 页面还没水合：这次输入被吞掉
        if self.selected: self.value, self.selected = "", False
        self.value += text; self.page.events.append(("type", text))
    def fill(self, text, timeout=None): self.page.events.append(("fill", text)); self.value = "" if self.page.fill_ignored else text
    def input_value(self, timeout=None): return self.value


class Line:
    def __init__(self, inp): self.inp = inp
    def is_visible(self): return True
    def count(self): return 1
    @property
    def first(self): return self
    def locator(self, sel):
        assert sel == U["min_input"]; return FakeList([self.inp])


class Ok:
    def __init__(self, pop): self.pop = pop
    def is_visible(self): return self.pop.is_visible()
    def count(self): return 1
    @property
    def first(self): return self
    def click(self, timeout=None):
        pg = self.pop.page; pg.events.append(("ok", self.pop.kind))
        if pg.drop_ok.get(self.pop.kind, 0) > 0: pg.drop_ok[self.pop.kind] -= 1; self.pop.open = False; return   # OK 被吞：面板关了但没生效
        self.pop.commit(); self.pop.open = False


class Popover:
    def __init__(self, page, kind):
        self.page, self.kind, self.open, self.ready_at = page, kind, False, 0
        self.inputs = [Input(page), Input(page)]
    def is_visible(self): return self.open and self.page.t >= self.ready_at
    def count(self): return 1
    @property
    def first(self): return self
    def locator(self, sel):
        if not self.is_visible(): return FakeList([])
        if self.kind == "price":
            if sel == U["min_input"]: return FakeList([self.inputs[0]])
            if sel == U["max_input"]: return FakeList([self.inputs[1]])
            if sel == U["ok_btn"]: return FakeList([Ok(self)])
            return FakeList([])
        if sel == U["total_sales_line"]: return FakeList([Line(self.inputs[0])])
        if sel == U["sales7_line"]: return FakeList([Line(self.inputs[1])])
        if sel == U["min_input"]: return FakeList(self.inputs)
        if sel == U["ok_btn"]: return FakeList([Ok(self)])
        return FakeList([])
    def commit(self):
        a, b = (i.value for i in self.inputs); ch = self.page.chips
        if self.kind == "price":
            ch.pop("price", None)
            if a and b: ch["price"] = f"Price: {a}~{b}"
            elif b: ch["price"] = f"Price: <{b}"
            elif a: ch["price"] = f"Price: {a}~"
        else:
            ch.pop("total", None); ch.pop("d7", None)
            if a: ch["total"] = f"Total sales: {a}~"
            if b: ch["d7"] = f"7-day sales: {b}~"


class Dropdown:
    def __init__(self, page, kind): self.page, self.kind = page, kind
    def is_visible(self): return True
    def count(self): return 1
    @property
    def first(self): return self
    def click(self, timeout=None):
        pg = self.page; pg.events.append(("open", self.kind)); pop = pg.pops[self.kind]
        for other in pg.pops.values():
            if other is not pop and not pg.keep_other_open: other.open = False
        pop.open, pop.ready_at = True, pg.t + pg.open_delay


class Text:
    def __init__(self, fn): self.fn = fn
    @property
    def first(self): return self
    def is_visible(self): return True
    def count(self): return 1
    def inner_text(self, timeout=None): return self.fn()


class FakePage:
    def __init__(self, keyword="cup", open_delay=0.0, lose_typing=0, drop_ok=None, keep_other_open=False, fill_ignored=False):
        self.t, self.events, self.keyword = 0.0, [], keyword
        self.open_delay, self.lose_typing, self.drop_ok = open_delay, lose_typing, dict(drop_ok or {})
        self.keep_other_open, self.fill_ignored = keep_other_open, fill_ignored
        self.pops = {"price": Popover(self, "price"), "sales": Popover(self, "sales")}
        self.chips = {}
        self.keyboard = self
    def press(self, key): self.events.append(("key", key))
    def sleep(self, s): self.t += s
    def wait_for_load_state(self, *a, **k): pass
    def selected(self):
        return "Selected:\n" + "\n×\n".join([f"Keyword: {self.keyword}"] + list(self.chips.values())) + "\n×\n×\nSaved filters"
    def evaluate(self, *a, **k): pass
    def locator(self, sel):
        if sel == U["price_dd"]: return Dropdown(self, "price")
        if sel == U["sales_dd"]: return Dropdown(self, "sales")
        if sel == U["popover"]: return FakeList([p for p in self.pops.values() if p.is_visible()])
        if sel == U["selected_row"]: return Text(self.selected)
        if sel == "body": return Text(lambda: "Find 7 products")
        return Sel(sel)


@pytest.fixture
def page(monkeypatch):
    holder = {}
    def mk(**kw):
        pg = FakePage(**kw); holder["pg"] = pg
        monkeypatch.setattr(W, "_fsleep", pg.sleep); monkeypatch.setattr(W, "_fnow", lambda: pg.t)
        monkeypatch.setattr(W.time, "sleep", pg.sleep)
        return pg
    return mk


def opens(pg, kind): return sum(1 for e in pg.events if e == ("open", kind))


# ---------- 等待面板就绪 ----------
def test_wait_popover_waits_until_panel_ready(page):
    pg = page(open_delay=3.0)
    W._click(pg, U, U["price_dd"])
    pop = W.wait_popover(pg, U, need=U["max_input"], avoid=U["total_sales_line"], timeout=10)
    assert pop is pg.pops["price"] and pg.t >= 3.0


def test_wait_popover_ignores_other_still_open_panel(page):
    pg = page(keep_other_open=True)
    pg.pops["price"].open = True                          # 上一个面板还在收起动画中
    W._click(pg, U, U["sales_dd"])
    pop = W.wait_popover(pg, U, need=U["total_sales_line"], timeout=5)
    assert pop is pg.pops["sales"]


def test_wait_popover_times_out_with_filter_not_applied(page):
    pg = page()
    with pytest.raises(W.FilterNotApplied):
        W.wait_popover(pg, U, need=U["max_input"], timeout=2)


# ---------- 真实键入 + 回读 ----------
def test_type_verified_retypes_when_first_input_swallowed(page):
    pg = page(lose_typing=1)
    inp = Input(pg)
    W.type_verified(inp, 8)
    assert inp.input_value() == "8" and sum(1 for e in pg.events if e == ("type", "8")) == 1


def test_type_verified_uses_key_events_not_fill(page):
    pg = page(fill_ignored=True)
    inp = Input(pg)
    W.type_verified(inp, 40)
    assert inp.input_value() == "40" and not any(isinstance(e, tuple) and e[0] == "fill" for e in pg.events)


def test_type_verified_gives_up_after_tries(page):
    pg = page(lose_typing=99)
    with pytest.raises(W.FilterNotApplied, match="8"):
        W.type_verified(Input(pg), 8, tries=3)


# ---------- 单个筛选：施加 → 校验 chip → 只重做这个筛选 ----------
def test_set_price_survives_slow_panel_and_swallowed_min(page):
    pg = page(open_delay=2.5, lose_typing=1)
    W.set_price(pg, U, F)
    assert PRICE_CHIP in pg.selected() and opens(pg, "price") == 1


def test_set_price_redoes_only_price_when_ok_swallowed(page):
    pg = page(drop_ok={"price": 1})
    W.set_price(pg, U, F)
    assert PRICE_CHIP in pg.selected() and opens(pg, "price") == 2 and opens(pg, "sales") == 0


def test_set_sales_redoes_when_ok_swallowed(page):
    pg = page(drop_ok={"sales": 2})
    W.set_sales(pg, U, F)
    sel = pg.selected()
    assert TOTAL_CHIP in sel and D7_CHIP in sel and opens(pg, "sales") == 3 and opens(pg, "price") == 0


def test_set_price_gives_up_after_three_tries(page):
    pg = page(drop_ok={"price": 99})
    with pytest.raises(W.FilterNotApplied, match="筛选未生效") as ei:
        W.set_price(pg, U, F)
    assert PRICE_CHIP in str(ei.value) and opens(pg, "price") == 3


def test_set_sales_then_price_chips_both_kept(page):
    pg = page(open_delay=1.5)
    W.set_price(pg, U, F); W.set_sales(pg, U, F)
    assert W.missing_chips(pg.selected(), [f"Keyword: {pg.keyword}", PRICE_CHIP, TOTAL_CHIP, D7_CHIP]) == []


# ---------- 查询数量阶段：筛选未生效 → 有上限地重新排队 ----------
def _count_run(monkeypatch, attempts, exc):
    def boom(kw, params): raise exc
    monkeypatch.setattr(W, "count_ehunt", boom)
    calls = []
    class St:
        def patch_run_if(s, rid, expect, **kw): calls.append(("if", expect, kw))
        def patch_run(s, rid, **kw): calls.append(("run", None, kw))
    class A: fake = None
    W.handle_run(St(), {"id": "r1", "keyword": "volufiline", "status": "counting", "attempts": attempts, "params": {}}, A(), False)
    return calls


@pytest.mark.parametrize("attempts", [1, 2])
def test_count_filter_not_applied_requeues(monkeypatch, attempts):
    calls = _count_run(monkeypatch, attempts, W.FilterNotApplied("筛选未生效，Selected: 缺少 ['Price: 8~40']"))
    (kind, expect, kw), = calls
    assert kind == "if" and expect == "counting"           # 用户已取消则不改回
    assert kw["status"] == "count_queued" and kw["locked_by"] is None and "筛选未生效" in kw["progress"]["why"]


def test_count_filter_not_applied_fails_after_two_requeues(monkeypatch):
    calls = _count_run(monkeypatch, W.COUNT_FILTER_REQUEUES + 1, W.FilterNotApplied("筛选未生效，Selected: 缺少 ['Price: 8~40']"))
    (kind, expect, kw), = calls
    assert kw["status"] == "failed" and "筛选未生效" in kw["error"] and "已自动重试 2 次" in kw["error"]


def test_count_other_error_still_fails_immediately(monkeypatch):
    calls = _count_run(monkeypatch, 1, RuntimeError("未识别结果数文字：x"))
    (kind, expect, kw), = calls
    assert kw["status"] == "failed" and "未识别" in kw["error"]


def test_count_filter_requeues_bounded_to_two():
    assert W.COUNT_FILTER_REQUEUES == 2


# ---------- 真 Playwright（本机 Chrome，独立临时浏览器，不碰 EHunt profile）：Locator API 与选择器语义 ----------
HTML = """<div class='el-popover' aria-hidden='false' style='display:none'><div><div>Total sales:</div><input placeholder='Min' id=stale></div></div>
<div class='el-popover' aria-hidden='false'><input placeholder='Min' id=pmin><input placeholder='Max' id=pmax><div role=button>OK</div></div>
<div class='el-popover' aria-hidden='false'><div><div>Total sales:</div><input placeholder='Min' id=ts></div><div><div>7-day sales:</div><input placeholder='Min' id=d7></div><div role=button>OK</div></div>
<script>document.querySelectorAll('input').forEach(i => i.addEventListener('input', () => { i.dataset.ev = (+(i.dataset.ev || 0)) + 1 }))</script>"""


@pytest.mark.skipif(not Path(W.SYSTEM_CHROME).exists(), reason="no system Chrome")
def test_real_playwright_targets_visible_panel_and_fires_input_events():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        try:
            pg = b.new_page(); pg.set_content(HTML)
            pop = W.wait_popover(pg, U, need=U["max_input"], avoid=U["total_sales_line"], timeout=3)
            W.type_verified(pop.locator(U["min_input"]).first, 8); W.type_verified(pop.locator(U["max_input"]).first, 40)
            sp = W.wait_popover(pg, U, need=U["total_sales_line"], timeout=3)   # 隐藏的旧面板也含 Total sales，不能选它
            W.type_verified(sp.locator(U["total_sales_line"]).locator(U["min_input"]).first, 100)
            W.type_verified(sp.locator(U["sales7_line"]).locator(U["min_input"]).first, 4)
            vals = dict(pg.eval_on_selector_all("input", "els => els.map(e => [e.id, [e.value, +(e.dataset.ev || 0)]])"))
            assert vals["pmin"][0] == "8" and vals["pmax"][0] == "40" and vals["ts"][0] == "100" and vals["d7"][0] == "4"
            assert all(vals[k][1] >= 1 for k in ("pmin", "pmax", "ts", "d7"))      # 真 input 事件（Vue v-model 能收到）
            assert vals["stale"] == ["", 0]
            assert pop.locator(U["ok_btn"]).count() == 1
        finally: b.close()
