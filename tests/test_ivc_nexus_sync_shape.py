"""IVC 那批包能不能走现有的包上传管线进 GitLab —— 用它们的真实形态验证。

数据源是 IVC 的 Nexus 索引
``.../nexus/content/repositories/releases/skills/index.json``（样本存在 workspace 的
``ivc-nexus-index-sample.json``），里面 17 个 skill + 1 个 agent。决定是 agent 走 zip/tar
包上传那条路，由门户自己存包进 GitLab，这样下载事件和版本号才记得到 —— 跳转 Nexus 的话
这些资产在报告里永远是 0 下载。

这里钉住的是**索引里的形状与管线的接口是否吻合**，四件事：

* 包是 ``.tar.gz``（``repro-expert-1.0.0-kiro.tar.gz``），而不是 zip。
* 版本号由 IVC 给（``1.0.0``），不能让管线自己从 1.0.0 起算 —— 否则同一个包同步两次会
  变成 1.0.0 和 1.1.0 两个版本。
* agent 的身份字段是 ``owner: "wenjie.a.liu"``，**不是工号**，也没有 ``display_name``；
  skill 那边是 ``author: "euhazhi"`` 加 ``display_name``。同步器必须把 owner 映射成 EID，
  映射不出来时不能瞎填一个人。
* ``created_at`` 索引里 18 条全是同一个值（1788170082 = 2026-08-31 09:54:42 UTC），
  它是索引生成时间而非资产创建时间，所以不能当创建日期写进库。

网络侧另有阻塞：生产宿主机到 ``100.78.105.106:8443`` 的出站被静默丢包（GitLab 和 Graph
同样从容器里发出去都能握手，所以是定向封锁而非全封）。抓取那一步在放行前无法验证，
所以这里用本地构造的包代替 Nexus 下载，验证的是下载之后的整条路。
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.category import RpAiCategory
from app.models.market import RpAiMarketItem
from app.models.market_version import RpAiMarketItemVersion
from app.services import gitlab_projects as glw

PROJECT_ID = 515151
# 索引里那个 agent 的原样字段。
IVC_AGENT = {
    "id": "repro-expert",
    "name": "repro-expert",
    "owner": "wenjie.a.liu",
    "version": "1.0.0",
    "created_at": "1788170082",
    "description": "客户站问题复现助手(六步工作流 + FMEA 瓶颈归因 + 全过程记录).",
    "tags": ["dcgm", "reproduction", "radio", "fmea", "circ"],
    "visibility": "internal",
    "deprecated": False,
    "runtimes": [
        {
            "runtime": "kiro",
            "download_url": (
                "https://arm1s10-eiffel239.eiffel.gic.ericsson.se:8443/nexus/content"
                "/repositories/releases/agents/repro-expert/1.0.0/kiro"
                "/repro-expert-1.0.0-kiro.tar.gz"
            ),
        }
    ],
}
# 同步器要用的映射：索引里的 owner 形态 -> 门户的 EID。
OWNER_TO_EID = {"wenjie.a.liu": "EWENJLI"}


def _targz(files: dict[str, str]) -> bytes:
    """一个 gzip 压缩的 tar，与 Nexus 上那个 -kiro.tar.gz 的形状一致。"""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return out.getvalue()


@pytest.fixture
def ivc_category():
    """同步进来的资产要落在一个真实存在的分组里。

    生产库里 ``ivc`` 这个分组已经有了（``a-replir-allocation-tool...`` 就挂在它下面），
    但测试库只种了 ``public``，所以这里补上 —— 断言的是同步逻辑，不是分组表的初始内容。
    """
    with SessionLocal() as db:
        if db.get(RpAiCategory, "ivc") is None:
            db.add(RpAiCategory(id="ivc", label="IVC"))
            db.commit()
    yield "ivc"


@pytest.fixture
def gitlab_stubbed(monkeypatch):
    """GitLab 侧打桩并记下推送内容，其余走真实代码：真解包、真写库、真算版本。"""
    pushed: dict = {}

    def fake_target(item_type: str) -> glw.UploadTarget:
        return glw.UploadTarget(
            item_type=item_type,
            group_path=f"rp-ai/rp-ai-user-{item_type}s",
            token="stub",
            env_var="STUB_TOKEN",
        )

    def fake_create(*, slug, name, description=None, target):
        pushed["created"] = {"slug": slug, "name": name, "group": target.group_path}
        return glw.ProjectRef(
            id=PROJECT_ID,
            path_with_namespace=f"{target.group_path}/{slug}",
            web_url=f"https://gitlab.example/{target.group_path}/{slug}",
            http_url=f"https://gitlab.example/{target.group_path}/{slug}.git",
        )

    def fake_publish(project, src, *, version, message, tmp_root, target):
        pushed["version"] = version
        pushed["message"] = message
        return "b" * 40, f"v{version}"

    monkeypatch.setattr(glw, "target_for", fake_target)
    monkeypatch.setattr(glw, "signing_args", lambda: [])
    monkeypatch.setattr(glw, "create_project", fake_create)
    monkeypatch.setattr(glw, "publish_tree", fake_publish)
    yield pushed
    with SessionLocal() as db:
        db.query(RpAiMarketItemVersion).filter(
            RpAiMarketItemVersion.item_id == f"gl-{PROJECT_ID}"
        ).delete(synchronize_session=False)
        row = db.get(RpAiMarketItem, f"gl-{PROJECT_ID}")
        if row:
            db.delete(row)
        db.commit()


def _package_bytes() -> bytes:
    return _targz(
        {
            "repro-expert/README.md": "# repro-expert\n\n## 启动\n```\npython main.py\n```\n",
            "repro-expert/main.py": "print('repro')\n",
            "repro-expert/.kiro/agents/repro.json": json.dumps({"name": "repro"}),
        }
    )


def _sync_upload(client, *, eid: str, version: str | None = None):
    """同步器会发的那一次请求：tar.gz + IVC 给的版本号 + 映射出来的 EID。"""
    data = {
        "eid": eid,
        "name": IVC_AGENT["name"],
        "categoryId": "ivc",
        "launch": "python main.py",
        "runtime": "oneshot",
        "itemType": "agent",
        "description": IVC_AGENT["description"],
    }
    if version is not None:
        data["version"] = version
    return client.post(
        "/api/market/agent-upload",
        data=data,
        files={
            "file": (
                "repro-expert-1.0.0-kiro.tar.gz",
                _package_bytes(),
                "application/gzip",
            )
        },
        headers={"X-User-Eid": eid},
    )


def test_a_nexus_targz_goes_through_the_upload_pipeline(client, gitlab_stubbed, ivc_category) -> None:
    """最基本的一条：IVC 的 .tar.gz 被接受，建项目、推送、上市。"""
    eid = OWNER_TO_EID[IVC_AGENT["owner"]]
    resp = _sync_upload(client, eid=eid)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == f"gl-{PROJECT_ID}"
    # 传完即上市：市场包上传不走安全审计，不会卡在 pending。
    assert body["status"] == "approved"
    assert gitlab_stubbed["created"]["group"] == "rp-ai/rp-ai-user-agents"

    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, f"gl-{PROJECT_ID}")
        assert item is not None
        assert item.item_type == "agent"
        assert item.source == "upload"
        assert item.creator == eid


def test_the_owner_is_mapped_to_an_eid_so_the_report_can_place_it(
    client, gitlab_stubbed, ivc_category
) -> None:
    """``owner`` 是 ``wenjie.a.liu``，报告只认工号。

    月报的「数字资产」一列按上传者工号关联组织树。creator 留空的条目归不到任何部门 ——
    生产库里已经有 5 项这种情况，同步不该再制造更多。索引里 17 个 skill 的 author 都是工号
    且 8 位作者全在 IVC，所以只有 agent 这一条需要映射。
    """
    eid = OWNER_TO_EID[IVC_AGENT["owner"]]
    assert eid.startswith("E") and eid.isupper()

    _sync_upload(client, eid=eid)

    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, f"gl-{PROJECT_ID}")
        assert item.creator == eid
        # 显示名走 creator_label：解析不出人名时退回工号，不会是空的。
        assert item.creator_label == eid


def test_the_index_version_is_used_rather_than_restarting_at_one(
    client, gitlab_stubbed, ivc_category
) -> None:
    """版本号必须沿用 IVC 的，否则同一个包同步两次会造出两个版本。

    索引里的版本跨度是 0.0.1 到 2.9.2，重新从 1.0.0 起算会同时对不上上游、
    并让「本月版本更新次数」这个指标凭空长出数字。
    """
    eid = OWNER_TO_EID[IVC_AGENT["owner"]]
    resp = _sync_upload(client, eid=eid, version=IVC_AGENT["version"])

    assert resp.status_code == 201, resp.text
    assert gitlab_stubbed["version"] == IVC_AGENT["version"]
    with SessionLocal() as db:
        versions = db.scalars(
            select(RpAiMarketItemVersion).where(
                RpAiMarketItemVersion.item_id == f"gl-{PROJECT_ID}"
            )
        ).all()
        assert [v.version for v in versions] == [IVC_AGENT["version"]]
        assert versions[0].tag == f"v{IVC_AGENT['version']}"


def test_the_index_created_at_is_the_same_for_every_entry() -> None:
    """所以它不能当资产的创建日期。

    这一条不碰接口，钉的是数据源本身的一个性质：18 条记录的 ``created_at`` 全等，说明它是
    索引生成时间。当成创建日期写进库的话，同步进来的资产会全部记成同一天新增，下一份月报的
    「本月新增资产」直接虚增 18。
    """
    sample = (
        Path("/workspace/home/ezhgmio/.kiro/crew/workspace")
        / "ivc-nexus-index-sample.json"
    )
    if not sample.exists():  # 样本不在这台机器上时跳过，断言的是数据源不是环境
        pytest.skip("index sample not present")
    index = json.loads(sample.read_text(encoding="utf-8"))
    stamps = {x["created_at"] for x in index["skills"] + index["agents"]}
    assert len(stamps) == 1, "如果上游开始给真实创建时间了，这条会失败，那时就该改用它"


def test_skill_and_agent_entries_do_not_share_a_shape() -> None:
    """两类要写两个映射器，不能共用一个。

    skill 有 ``author`` / ``display_name`` / ``download_url`` / ``skill_md_url``；
    agent 换成 ``owner`` 和 ``runtimes[].download_url``，并多出
    ``deprecated`` / ``visibility`` / ``dependencies`` / ``compatibility``。
    """
    sample = (
        Path("/workspace/home/ezhgmio/.kiro/crew/workspace")
        / "ivc-nexus-index-sample.json"
    )
    if not sample.exists():
        pytest.skip("index sample not present")
    index = json.loads(sample.read_text(encoding="utf-8"))
    skill_fields = set().union(*(set(x) for x in index["skills"]))
    agent_fields = set().union(*(set(x) for x in index["agents"]))

    assert {"author", "display_name", "download_url", "skill_md_url"} <= skill_fields
    assert "author" not in agent_fields and "owner" in agent_fields
    assert "download_url" not in agent_fields  # 藏在 runtimes[] 里
    assert index["agents"][0]["runtimes"][0]["download_url"].endswith(".tar.gz")
