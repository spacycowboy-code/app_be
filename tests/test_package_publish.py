"""发布一个包:传完即上市,而且这条路第一次有端到端覆盖。

在这之前 `publish_package` 一条测试都没有 —— 它在生产也从没跑成过(推送被签名规则挡了),
所以两个洞是一起被发现的:`_ensure_user` 拿 EID 当整数主键查、每次上传插一行重复 eid;以及
`status` 的口径。这里把整条路钉住,GitLab 那一侧打桩(建项目、推送),其余都是真的:真的解包、
真的写库、真的算版本号。

**传完即上市。** 原来是 `pending` 等 admin 批。那个闸门的口径反了(登记一个可以指向任何仓库
的地址是即时上市的,而门户唯一能看见内容的这条路要等审批),而且它不运转(能批的只有一个
EID、还是 break-glass 那个账号,且上传后零通知)。换来的口径是"同事放的,自己看":每一行有
creator、每一版有 GitLab 提交,作者和管理员都能自己下架。
"""
from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.models.market_version import RpAiMarketItemVersion
from app.models.user import RpAiPortal
from app.services import gitlab_projects as glw

OWNER = "EZHGMIO"
PROJECT_ID = 424242


def _zip(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in files.items():
            zf.writestr(path, text)
    return out.getvalue()


@pytest.fixture
def gitlab_stubbed(monkeypatch):
    """GitLab 那一侧打桩,把推送记下来。别的都走真的代码。"""
    pushed: dict = {}

    def fake_target(item_type: str) -> glw.UploadTarget:
        return glw.UploadTarget(
            item_type=item_type, group_path=f"rp-ai/rp-ai-user-{item_type}s",
            token="stub", env_var="STUB_TOKEN",
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
        return "a" * 40, f"v{version}"

    monkeypatch.setattr(glw, "target_for", fake_target)
    monkeypatch.setattr(glw, "signing_args", lambda: [])
    monkeypatch.setattr(glw, "create_project", fake_create)
    monkeypatch.setattr(glw, "publish_tree", fake_publish)
    return pushed


def _upload(client, *, item_type: str = "tool", name: str = "Generate EEF Report"):
    return client.post(
        "/api/market/agent-upload",
        data={
            "eid": OWNER, "name": name, "categoryId": "public",
            "launch": "python main.py", "runtime": "oneshot", "itemType": item_type,
        },
        files={"file": ("pkg.zip", _zip({
            "my-tool/README.md": "# my-tool\n\nDoes a thing.\n",
            "my-tool/main.py": "print(1)\n",
        }), "application/zip")},
        headers={"X-User-Eid": OWNER},
    )


def test_an_uploaded_package_is_on_the_market_immediately(client, gitlab_stubbed) -> None:
    r = _upload(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "approved"          # 不是 pending
    assert body["id"] == f"gl-{PROJECT_ID}"
    assert body["version"] == "1.0.0"

    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, f"gl-{PROJECT_ID}")
        assert item is not None
        assert item.status == "approved"
        assert item.source == "upload"
        assert item.item_type == "tool"
        assert item.creator == OWNER


def test_everyone_can_see_it_without_anybody_approving(client, gitlab_stubbed) -> None:
    """上市的定义是别人看得见。原来这里要 admin 先点一下,而没有人会被通知去点。"""
    _upload(client)
    ids = [i["id"] for i in client.get("/api/market", headers={"X-User-Eid": "EOTHER1"}).json()]
    assert f"gl-{PROJECT_ID}" in ids
    # 匿名也算:市场是内部公开的。
    assert f"gl-{PROJECT_ID}" in [i["id"] for i in client.get("/api/market").json()]


def test_the_version_row_and_the_uploader_row_are_written_once(client, gitlab_stubbed) -> None:
    """版本行是上传的留痕,而上传者那一行是它的外键 —— 后者曾经每次上传都插一行重复 eid。"""
    _upload(client)
    with SessionLocal() as db:
        versions = db.scalars(
            select(RpAiMarketItemVersion).where(
                RpAiMarketItemVersion.item_id == f"gl-{PROJECT_ID}"
            )
        ).all()
        assert len(versions) == 1
        assert versions[0].version == "1.0.0"
        assert versions[0].uploader_eid == OWNER
        assert versions[0].tag == "v1.0.0"
        # 审计已经不跑了,所以这一版没有留痕。
        assert versions[0].audit_json is None
        # 上传者那一行:一条,不是两条。
        assert len(db.scalars(select(RpAiPortal).where(RpAiPortal.eid == OWNER)).all()) == 1


def test_a_tool_goes_to_the_tools_group_an_agent_to_the_agents_group(
    client, gitlab_stubbed
) -> None:
    """itemType 决定的两件事之一。组和凭据是同一个决定,搞错了不报错、条目也正常。"""
    _upload(client, item_type="agent", name="Some Agent")
    assert gitlab_stubbed["created"]["group"] == "rp-ai/rp-ai-user-agents"


def test_taking_it_down_hides_it_from_everyone_but_the_uploader(client, gitlab_stubbed) -> None:
    """传完即上市换来的代价是下架必须真的管用,而且要即时。"""
    _upload(client)
    r = client.post(
        f"/api/market/gl-{PROJECT_ID}/review",
        json={"status": "rejected"},
        headers={"X-User-Eid": "ADMINMM"},
    )
    assert r.status_code == 200
    assert f"gl-{PROJECT_ID}" not in [
        i["id"] for i in client.get("/api/market", headers={"X-User-Eid": "EOTHER1"}).json()
    ]
    # 上传者仍然看得到自己那一条 —— 否则他不知道发生了什么。
    assert f"gl-{PROJECT_ID}" in [
        i["id"] for i in client.get("/api/market", headers={"X-User-Eid": OWNER}).json()
    ]
