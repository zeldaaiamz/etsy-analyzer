import pytest
from analyzer.providers import ehunt


@pytest.fixture(autouse=True)
def _fast_ehunt(monkeypatch):
    """测试中关闭 EHunt 全局限速间隔与 429 退避等待。"""
    monkeypatch.setattr(ehunt, "MIN_INTERVAL", 0)
    monkeypatch.setattr(ehunt, "_sleep", lambda s: None)


@pytest.fixture(autouse=True)
def _fresh_net_breaker(monkeypatch):
    """选品 worker 的网络熔断器是模块级全局：每个测试用新的，避免前一个测试的失败计数让 main() 循环进入冷却。"""
    import sys
    W = sys.modules.get("selection.ehunt_worker")
    if W is not None and hasattr(W, "NetBreaker"): monkeypatch.setattr(W, "NET", W.NetBreaker())
