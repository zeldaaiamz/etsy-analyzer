"""① 就绪判定的真浏览器回放测试（本地假 EHunt 页面，不连真实 EHunt、不用 worker 的 profile）：
页面 1.5 秒后出现搜索框和结果列表，但一直挂着一个长连接请求（模拟统计/客服长轮询或卡住的出口）→ 旧的 networkidle 永远等不到；
新的 goto_ready 应在几秒内 ready。另模拟断网（请求被 abort）→ 抛网络类错误、文案「box 网络异常」。"""
import http.server, socketserver, tempfile, threading, time, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection import ehunt_worker as W  # noqa: E402

pw = pytest.importorskip("playwright.sync_api")

PAGE = b"""<!doctype html><html><head><title>Top Etsy Product Research Tool</title></head><body><div id=app>loading</div>
<script>
fetch('/hang').catch(()=>{});                       // never finishes -> networkidle never reached
setTimeout(()=>{ document.getElementById('app').innerHTML =
 "<input placeholder='Enter a product name, tag, product ID or shop name'>" +
 "<table class='el-table__body'><tr class='el-table__row'><td><a class='product-title-clamp' href='https://www.etsy.com/listing/1'>x</a></td></tr></table>"; }, 1500);
</script></body></html>"""
LOGIN = b"<!doctype html><html><head><title>Login - EHunt</title></head><body>Sign in</body></html>"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/hang"): time.sleep(120); return
        body = PAGE if self.path.startswith("/etsy-product-research") else LOGIN
        if self.path.startswith("/guest"):
            self.send_response(302); self.send_header("Location", "/login"); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    class S(socketserver.ThreadingMixIn, http.server.HTTPServer): daemon_threads = True
    s = S(("127.0.0.1", 0), H); threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()


@pytest.fixture(scope="module")
def page():
    with pw.sync_playwright() as p:
        try: ctx = p.chromium.launch_persistent_context(tempfile.mkdtemp(prefix="netfix-test-"), channel="chrome", headless=True)
        except Exception as e: pytest.skip(f"no chrome: {e}")
        pg = ctx.new_page(); yield pg; ctx.close()


def test_old_networkidle_times_out_but_goto_ready_is_fast(server, page):
    U = W.ui()
    with pytest.raises(Exception, match="Timeout"):
        page.goto(server + "/etsy-product-research", wait_until="networkidle", timeout=6000)
    t = time.monotonic(); assert W.goto_ready(page, U, url=server + "/etsy-product-research", timeout_s=20) == "ready"
    took = time.monotonic() - t
    assert took < 6, took
    assert page.locator(U["row"]).count() == 1


def test_redirect_to_login_page_is_login(server, page):
    assert W.goto_ready(page, W.ui(), url=server + "/guest", timeout_s=10) == "login"


def test_network_down_is_box_network(server, page):
    page.context.route("**/*", lambda r: r.abort("internetdisconnected"))
    try:
        with pytest.raises(Exception) as ei: W.goto_ready(page, W.ui(), url=server + "/etsy-product-research", timeout_s=10)
    finally: page.context.unroute("**/*")
    assert W.is_net_error(ei.value) and W.net_label(ei.value) == "box 网络异常"
