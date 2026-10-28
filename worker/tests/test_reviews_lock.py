"""reviews-start 单实例锁：并发/重复 start 只会起一个评论 worker；stop 后锁释放可再启动。
用临时 HOME + 假 review_scraper（sleep），不碰真实 worker / profile。"""
import os, shutil, subprocess, time, pathlib, signal
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("flock") is None or shutil.which("setsid") is None, reason="需要 flock/setsid")



def test_reviews_start_single_instance(tmp_path):
    root = tmp_path / "repo"; (root / "deploy").mkdir(parents=True); (root / "worker" / "selection").mkdir(parents=True)
    shutil.copy(REPO / "deploy" / "box-selection-worker.sh", root / "deploy")
    (root / "worker" / "selection" / "__init__.py").write_text("")
    (root / "worker" / "selection" / "review_scraper.py").write_text(
        f"import time, sys\nsys.argv[0] = 'fake-reviews-{tmp_path.name}'\ntime.sleep(60)\n")
    home = tmp_path; env = {**os.environ, "HOME": str(home), "SEL_ENV_FILE": str(tmp_path / "none.env")}
    sh = [str(root / "deploy" / "box-selection-worker.sh")]
    try:
        # 模拟 ensure-worker.sh 与 watch 同时 reviews-start；再加一次删掉 pid 文件后的 start（只靠锁挡住）
        ps = [subprocess.Popen(sh + ["reviews-start"], env=env, stdout=subprocess.PIPE, text=True) for _ in range(4)]
        outs = [p.communicate(timeout=20)[0] for p in ps]
        assert sum("reviews started" in o for o in outs) == 1, outs
        time.sleep(1.5)
        (home / ".local/share/etsy-selection/reviews.pid").rename(home / "pid.bak")
        o = subprocess.run(sh + ["reviews-start"], env=env, capture_output=True, text=True, timeout=20).stdout
        assert "lock" in o and "already running" in o, o
        (home / "pid.bak").rename(home / ".local/share/etsy-selection/reviews.pid")
        pgid = int((home / ".local/share/etsy-selection/reviews.pid").read_text())
        n = subprocess.run(["pgrep", "-g", str(pgid), "-f", "selection.review_scraper"], capture_output=True, text=True).stdout.split()
        assert len(n) == 2, n   # bash 守护循环 + python，各一个
        assert "stopped" in subprocess.run(sh + ["reviews-stop"], env=env, capture_output=True, text=True).stdout
        time.sleep(1)
        o = subprocess.run(sh + ["reviews-start"], env=env, capture_output=True, text=True, timeout=20).stdout
        assert "reviews started" in o, o   # 停止后锁已释放
    finally:
        pf = home / ".local/share/etsy-selection/reviews.pid"
        if pf.exists():
            try: os.killpg(int(pf.read_text()), signal.SIGTERM)
            except ProcessLookupError: pass
