"""清理「没有包可下」留下的下载流水：``tools/prune_phantom_market_downloads.py``。

这个脚本会**永久删掉生产数据**，所以判据必须被钉死。两条最要命的：

* **默认不写库。** dry run 是默认值，不是一个可选项 —— 一个默认就删的脚本迟早会被人不带参数跑一次。
* **上游托管的条目不能被误删。** 它们的 id 也不是 ``gl-`` 开头，但下载是**通的**，那些行是真实
  下载。只按 id 前缀判会把这批真数据一起销毁，而且删完无从恢复。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.models.market_download import RpAiMarketDownload

TOOL = Path(__file__).resolve().parents[1] / "tools" / "prune_phantom_market_downloads.py"
NEXUS = "https://nexus.example/agents/repro-expert/1.2.0/kiro/repro-expert.tar.gz"


def _item(item_id: str, **over) -> RpAiMarketItem:
    kw = {
        "id": item_id,
        "name": item_id,
        "item_type": "agent",
        "service_type": "code",
        "category_id": "public",
        "status": "approved",
    }
    kw.update(over)
    return RpAiMarketItem(**kw)


@pytest.fixture()
def seeded():
    """三类流水各一批：GitLab 同步的、上游托管的、什么都没有的。"""
    with SessionLocal() as db:
        db.add_all(
            [
                _item("gl-142925", source="gitlab-agents"),
                _item("ivc-repro-expert", source="nexus-ivc", external_url=NEXUS,
                      external_source="nexus-ivc"),
                _item("manual-thing", source="manual", service_url="https://example/x"),
            ]
        )
        db.flush()
        db.add_all(
            [
                RpAiMarketDownload(item_id="gl-142925", eid="EAAA"),
                RpAiMarketDownload(item_id="gl-142925", eid="EBBB"),
                RpAiMarketDownload(item_id="ivc-repro-expert", eid="ECCC"),
                RpAiMarketDownload(item_id="manual-thing", eid="EDDD"),
                RpAiMarketDownload(item_id="manual-thing", eid="EDDD"),
                RpAiMarketDownload(item_id="manual-thing", eid="EEEE"),
            ]
        )
        db.commit()
    yield


def _run(*args: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, timeout=300,
        cwd=str(TOOL.parents[1]),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _ids(db) -> list[str]:
    return sorted(d.item_id for d in db.scalars(select(RpAiMarketDownload)))


def test_dry_run_is_the_default_and_deletes_nothing(seeded):
    """一个默认就删的脚本迟早会被人不带参数跑一次。"""
    out = _run()
    assert "dry run" in out
    with SessionLocal() as db:
        assert len(_ids(db)) == 6


def test_dry_run_names_the_items_and_the_visible_change(seeded):
    """卡片上显示的是去重人数，所以删 3 行可能只让卡片少 2 —— 两个数都要给。"""
    out = _run()
    assert "manual-thing" in out
    # manual-thing：3 行、2 个不同的人。
    assert "3" in out and "2 -> 0" in out


def test_apply_removes_only_the_unpackaged_ones(seeded):
    _run("--apply")
    with SessionLocal() as db:
        assert _ids(db) == ["gl-142925", "gl-142925", "ivc-repro-expert"]


def test_an_externally_hosted_item_is_never_touched(seeded):
    """它的 id 也不是 gl- 开头，但下载是通的 —— 那一行是真实下载。

    只按 id 前缀判会把这批真数据一起销毁。这条测试是这个脚本能不能上生产的唯一门槛。
    """
    _run("--apply")
    with SessionLocal() as db:
        rows = [d for d in db.scalars(select(RpAiMarketDownload))
                if d.item_id == "ivc-repro-expert"]
        assert len(rows) == 1


def test_a_gitlab_item_is_never_touched(seeded):
    """取包失败也记流水是有意的：「大家都下不下来」必须能从计数里看出来。"""
    _run("--apply")
    with SessionLocal() as db:
        rows = [d for d in db.scalars(select(RpAiMarketDownload))
                if d.item_id == "gl-142925"]
        assert len(rows) == 2


def test_running_twice_is_a_no_op(seeded):
    _run("--apply")
    out = _run("--apply")
    assert "无事可做" in out
    with SessionLocal() as db:
        assert len(_ids(db)) == 3


def test_with_nothing_to_clean_it_says_so():
    out = _run()
    assert "无事可做" in out
