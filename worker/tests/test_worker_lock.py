"""选品 worker start 单实例锁：并发/重复 start 只起一个 supervisor；删 pid 文件后第二次 start 也被锁挡住；
直接 _supervise 兜底也起不来；stop 后锁释放可再启动。临时 HOME + 假 ehunt_worker（sleep），不碰真实 worker / profile。"""
import os, shutil, subprocess, time, pathlib, signal
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("flock") is None, reason="需要 flock")


def test_worker_start_single_instance(tmp_path):
    root = tmp_path / "repo"; (root / "deploy").mkdir(parents=True); (root / "worker" / "selection").mkdir(parents=True)
    shutil.copy(REPO / "deploy" / "box-selection-worker.sh", root / "deploy")
    (root / "worker" / "selection" / "__init__.py").write_text("")
    (root / "worker" / "selection" / "ehunt_worker.py").write_text("import time\ntime.sleep(60)\n")
    home = tmp_path; env = {**os.environ, "HOME": str(home), "SEL_ENV_FILE": str(tmp_path / "none.env")}
    data = home / ".local/share/etsy-selection"
    sh = [str(root / "deploy" / "box-selection-worker.sh")]
    run = lambda *a: subprocess.run(sh + list(a), env=env, capture_output=True, text=True, timeout=30).stdout
    try:
        ps = [subprocess.Popen(sh + ["start"], env=env, stdout=subprocess.PIPE, text=True) for _ in range(4)]
        outs = [p.communicate(timeout=20)[0] for p in ps]
        assert sum("started" in o and "already" not in o for o in outs) == 1, outs
        time.sleep(1.5)
        (data / "worker.pid").rename(home / "pid.bak")
        o = run("start"); assert "lock" in o and "already running" in o, o          # 第二次 start 起不来
        o = run("ensure"); assert o == "", o                                       # ensure 靠锁探测，不重复启动
        assert "lock held" in run("status")
        r = subprocess.run(sh + ["_supervise"], env=env, capture_output=True, text=True, timeout=10)
        assert r.returncode == 0 and "held by another supervisor" in r.stdout, r.stdout   # _supervise 兜底
        (home / "pid.bak").rename(data / "worker.pid")
        assert "stopped" in run("stop")
        time.sleep(1)
        assert "not running" in run("status")
        o = run("start"); assert "started" in o and "already" not in o, o         # 停止后锁已释放
    finally:
        pf = data / "worker.pid"
        if pf.exists():
            try: os.kill(int(pf.read_text()), signal.SIGTERM)
            except ProcessLookupError: pass
