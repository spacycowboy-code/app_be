"""谁能看 Admin -> RSW 那一页，以及它为什么不是 dashboard 那张名单。

这次拆分的全部意义在于一个**不对称**关系：能看逐人使用报告的人都能看 RSW 页，反过来不行。
把它写成测试而不是只写进注释，是因为两张名单合并回一张不会报错 —— 症状是某个只该管
RSW 资产的人忽然能看到 950 人的姓名与用量，而那是一次静默的权限扩大。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.services.eids import require_dashboard_eid, require_rsw_page_eid


class _Req:
    """最小的请求替身：这两个 require 只读 ``request.state.eid``。"""

    class _State:
        def __init__(self, eid: str | None) -> None:
            self.eid = eid

    def __init__(self, eid: str | None) -> None:
        self.state = self._State(eid)


def test_the_rsw_page_list_is_a_superset_of_the_dashboard_list():
    cfg = Settings()
    dashboard = cfg.dashboard_eid_list
    rsw = cfg.rsw_page_eid_list
    # 超集：报告看得到的人不必为了点一次同步再被单独加一遍。
    assert set(dashboard) <= set(rsw)
    # 且真的更宽 —— 相等就说明这次拆分没生效，那正是这条要防的退化。
    assert set(rsw) - set(dashboard)


def test_ecuiden_gets_the_rsw_page_and_not_the_per_person_report():
    """这条就是这次改动要的那个结果，写成两个方向。

    只断言她在 RSW 名单里是不够的：把 `rsw_extra_page_eids` 的内容原样塞进
    `dashboard_eids` 同样能让第一句通过，而那恰好是 2026-09-02 撤销过的做法。
    """
    cfg = Settings()
    assert "ECUIDEN" in cfg.rsw_page_eid_list
    assert "ECUIDEN" not in cfg.dashboard_eid_list


def test_the_list_has_no_duplicates_and_keeps_its_order():
    cfg = Settings()
    rsw = cfg.rsw_page_eid_list
    assert len(rsw) == len(set(rsw))
    # dashboard 的人排在前面、顺序不变；额外的人接在后面。
    assert rsw[: len(cfg.dashboard_eid_list)] == cfg.dashboard_eid_list


def test_extra_eids_are_normalised_like_every_other_eid_list():
    cfg = Settings(rsw_extra_page_eids=" ecuiden , ,ezhgmio ")
    # 小写被抬成大写、空段被丢掉，且 ezhgmio 已在 dashboard 名单里所以不重复出现。
    assert cfg.rsw_page_eid_list.count("EZHGMIO") == 1
    assert "ECUIDEN" in cfg.rsw_page_eid_list


def test_an_empty_extra_list_leaves_the_two_lists_identical():
    """把额外名单清空必须退化成旧行为，而不是把所有人放进来。"""
    cfg = Settings(rsw_extra_page_eids="")
    assert cfg.rsw_page_eid_list == cfg.dashboard_eid_list


def test_require_rsw_page_eid_lets_the_extra_person_through():
    assert require_rsw_page_eid(_Req("ECUIDEN"), "同步 Gerrit 资产") == "ECUIDEN"
    # 同一个人打报告那道闸门必须被挡住 —— 这是两个 require 的分工。
    with pytest.raises(HTTPException) as exc:
        require_dashboard_eid(_Req("ECUIDEN"), "看使用报告")
    assert exc.value.status_code == 403


def test_require_rsw_page_eid_still_refuses_everyone_else():
    with pytest.raises(HTTPException) as exc:
        require_rsw_page_eid(_Req("ENOBODY"), "同步 Gerrit 资产")
    assert exc.value.status_code == 403
    # 动作名要进 403，否则用户只知道"不行"、不知道是哪一个管理面不行。
    assert "同步 Gerrit 资产" in exc.value.detail


def test_a_missing_identity_is_401_not_403():
    """没登录和登录了但没权限是两件事，措辞不同、用户的下一步也不同。"""
    for eid in (None, "", "   "):
        with pytest.raises(HTTPException) as exc:
            require_rsw_page_eid(_Req(eid), "同步 Gerrit 资产")
        assert exc.value.status_code == 401


def test_lowercase_identity_is_accepted():
    # request.state.eid 的大小写不该决定一个人有没有权限。
    assert require_rsw_page_eid(_Req("ecuiden"), "同步 Gerrit 资产") == "ECUIDEN"
