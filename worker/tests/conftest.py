import pytest
from analyzer.providers import ehunt


@pytest.fixture(autouse=True)
def _fast_ehunt(monkeypatch):
    """测试中关闭 EHunt 全局限速间隔与 429 退避等待。"""
    monkeypatch.setattr(ehunt, "MIN_INTERVAL", 0)
    monkeypatch.setattr(ehunt, "_sleep", lambda s: None)
