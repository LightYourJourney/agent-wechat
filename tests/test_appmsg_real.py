# tests/test_appmsg_real.py
"""真机冒烟：规范化后的 appmsg 可解析率。**这个测试的存在理由就是证伪「content 是纯 XML」。**

需要本机已登录微信且已建索引；`pyproject.toml` 的 addopts 已按惯例 deselect `*_real.py`。
不计具体条数（微信库是活的），只断言比率下界。
"""
import sqlite3
import xml.etree.ElementTree as ET

import pytest

from agent_wechat import account as account_mod
from agent_wechat import appmsg, reader


def _account() -> str:
    """与生产同源地取当前账号。

    **不要用 `paths.account_dirs()[0]`**：多账号各有一份 index，排第一的不一定是
    索引所属的那个，会让本用例读到另一份数据而静默通过（或莫名失败）。
    """
    try:
        return account_mod.resolve_account(None)
    except Exception:
        pytest.skip("本机没有可解析的登录账号")


def test_real_appmsg_parse_rate_after_normalize():
    """回归：**不做规范化**时这个比率是 0.33 左右；做了应当 >= 0.99。

    这条断言能失败：把 `normalize` 里的 `_GROUP_PREFIX` 那行删掉，比率会掉回 0.33。
    """
    r = reader.MessageReader(_account())
    total = ok = 0
    for _sid, _key, raw in r.iter_parts_input():
        if raw is None:
            continue
        total += 1
        if not appmsg.is_dangerous(raw):
            try:
                ET.fromstring(appmsg.normalize(raw))
                ok += 1
            except ET.ParseError:
                pass
        if total >= 5000:      # 抽样即可，别把整套测试拖到分钟级
            break
    assert total > 0, "一条 appmsg 都没扫到，索引可能是空的"
    rate = ok / total
    assert rate >= 0.99, f"规范化后可解析率 {rate:.4f}，远低于实测的 0.99998"
