"""包能改了:换内容(新版本)和改启动信息(不换内容),两条路都钉住。

在这之前市场里的 tool/agent 包**一旦传上去就是定稿**:换内容那条路后端其实是全的(带
`itemId` 就复用同一个 GitLab 项目、追加一行版本行),但前端从来没绑过那个字段,界面上到不了;
而启动命令、runtime、keep、前置条件这四项存在版本行的 `metadata_json` 里,唯一的写入者是
一次新上传 —— 填错一个字,作者只能把包重新打一遍。

这里覆盖四件容易悄悄错的事:

1. **更新不改条目名。** 两个入口(卡片上的「编辑」改说明,这里换内容)原来共用一个表单,
   于是传个新版本会顺手把条目名改成对话框里的值。前端锁成只读只是提示,后端不读它才是边界。
2. **版本号由人填,且查重。** tag 是 `v<版本>`,重复的版本号会让 push 在最后一步被 GitLab
   拒 —— 那时内容已经进了 main、库里却没记这一版。所以在收包之前就拒。
3. **手工登记的条目不能上传新版本。** 原来会往下走到"新建一个 GitLab 项目",而 `repo_url`
   只在新建条目那条分支里写:条目仍指向旧地址,包躺在一个没人认领的新项目里。
4. **改启动信息时 sidecar 要改在原来那个位置。** 包自带一层顶层目录时(绝大多数人这么打包)
   sidecar 在那一层里面,写死到仓库根会留下两份,而拿到 zip 的人看到的是没被改的那一份。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.models.market_version import RpAiMarketItemVersion
from app.routers import agent_upload as au
from app.services import agent_package as pkg
from app.services import gitlab_projects as glw

OWNER = "EZHGMIO"
OTHER = "EJIACAO"
PROJECT_ID = 515151
ITEM_ID = f"gl-{PROJECT_ID}"


def _zip(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in files.items():
            zf.writestr(path, text)
    return out.getvalue()


@pytest.fixture
def gitlab_stubbed(monkeypatch):
    """GitLab 那一侧打桩。`pushed["src"]` 留下这次推上去的工作目录,好断言 sidecar。"""
    pushed: dict = {}

    def fake_target(item_type: str) -> glw.UploadTarget:
        return glw.UploadTarget(
            item_type=item_type, group_path=f"rp-ai/rp-ai-user-{item_type}s",
            token="stub", env_var="STUB_TOKEN",
        )

    def fake_create(*, slug, name, description=None, target):
        pushed["created"] = {"slug": slug, "name": name}
        return glw.ProjectRef(
            id=PROJECT_ID,
            path_with_namespace=f"{target.group_path}/{slug}",
            web_url=f"https://gitlab.example/{target.group_path}/{slug}",
            http_url=f"https://gitlab.example/{target.group_path}/{slug}.git",
        )

    def fake_publish(project, src, *, version, message, tmp_root, target):
        pushed["version"] = version
        pushed["message"] = message
        # publish_tree 之后调用方会删掉临时目录,所以内容在这里就读出来。
        pushed["tree"] = {
            str(p.relative_to(src)): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(Path(src).rglob("*")) if p.is_file()
        }
        return "b" * 40, f"v{version}"

    monkeypatch.setattr(glw, "target_for", fake_target)
    monkeypatch.setattr(glw, "signing_args", lambda: [])
    monkeypatch.setattr(glw, "create_project", fake_create)
    monkeypatch.setattr(glw, "publish_tree", fake_publish)
    return pushed


def _first_upload(client, *, name: str = "Revise Me") -> str:
    r = client.post(
        "/api/market/agent-upload",
        data={
            "eid": OWNER, "name": name, "categoryId": "public",
            "launch": "python main.py", "runtime": "oneshot", "itemType": "tool",
        },
        files={"file": ("pkg.zip", _zip({
            "my-tool/README.md": "# my-tool\n",
            "my-tool/main.py": "print(1)\n",
        }), "application/zip")},
        headers={"X-User-Eid": OWNER},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _new_version(client, item_id: str, *, version: str | None, name: str = "Revise Me"):
    # 刻意**不带** categoryId / description / tags:前端在这个模式下不发它们,而后端也不该
    # 要求 —— 一个部门没有对应分组的上传者不该在"传个新版本"时被要求选分组。
    data = {
        "eid": OWNER, "name": name,
        "launch": "python main.py --v2", "runtime": "oneshot", "itemType": "tool",
        "itemId": item_id,
    }
    if version is not None:
        data["version"] = version
    return client.post(
        "/api/market/agent-upload",
        data=data,
        files={"file": ("pkg.zip", _zip({
            "my-tool/README.md": "# my-tool v2\n",
            "my-tool/main.py": "print(2)\n",
        }), "application/zip")},
        headers={"X-User-Eid": OWNER},
    )


# --- 换内容(上传新版本) --------------------------------------------------


def test_a_new_version_needs_an_explicit_version_number(client, gitlab_stubbed) -> None:
    """自动进位在更新这条路上是错的:只有作者知道这一版是不是不兼容上一版。"""
    item_id = _first_upload(client)
    r = _new_version(client, item_id, version=None)
    assert r.status_code == 400
    assert "版本号" in r.json()["detail"]


def test_a_new_version_does_not_rename_the_item(client, gitlab_stubbed) -> None:
    item_id = _first_upload(client, name="Revise Me")
    r = _new_version(client, item_id, version="2.0.0", name="Sneaky New Name")
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, item_id)
        assert item.name == "Revise Me"      # 不是 Sneaky New Name
        assert item.version == "2.0.0"       # 版本号照旧要更新
    # 包里的 sidecar 也用条目上那个名字,否则 zip 拿到别处和市场上显示的对不上。
    sidecar = gitlab_stubbed["tree"][f"my-tool/{pkg.SIDECAR_NAME}"]
    assert '"Revise Me"' in sidecar
    assert "Sneaky" not in sidecar


def test_a_new_version_keeps_the_items_own_metadata(client, gitlab_stubbed) -> None:
    """描述、分组、标签归卡片上的「编辑」管。这条路不带它们,也不该被默认值覆盖。"""
    item_id = _first_upload(client)
    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, item_id)
        item.description = "原来的说明"
        item.tags = ["keep", "these"]
        before = (item.category_id, item.category, item.author)
        db.commit()
    assert _new_version(client, item_id, version="1.2.0").status_code == 201
    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, item_id)
        assert item.description == "原来的说明"
        assert item.tags == ["keep", "these"]
        assert (item.category_id, item.category, item.author) == before


def test_a_version_number_cannot_be_reused(client, gitlab_stubbed) -> None:
    """撞了要在收包之前拒:tag 撞在 push 的最后一步,那时内容已经进 main 了。"""
    item_id = _first_upload(client)
    r = _new_version(client, item_id, version="1.0.0")
    assert r.status_code == 400
    assert "1.0.0" in r.json()["detail"]


@pytest.mark.parametrize("bad", ["1.0 0", "-1.0", "1..0", "v", "release/2", "vv2.0"])
def test_a_version_number_must_be_usable_as_a_git_tag(client, gitlab_stubbed, bad) -> None:
    item_id = _first_upload(client)
    r = _new_version(client, item_id, version=bad)
    assert r.status_code == 400, f"{bad!r} 应该被拒"


def test_a_leading_v_is_accepted_and_normalised(client, gitlab_stubbed) -> None:
    """人会写 v2.0.0。tag 本来就会加 v,原样收下会得到 vv2.0.0。"""
    item_id = _first_upload(client)
    r = _new_version(client, item_id, version="v2.0.0")
    assert r.status_code == 201, r.text
    assert r.json()["version"] == "2.0.0"
    assert r.json()["tag"] == "v2.0.0"


def test_a_manually_registered_item_cannot_take_an_uploaded_version(
    client, gitlab_stubbed
) -> None:
    """id 不是 gl- 的条目没有属于它的项目;原来这里会静默新建一个。"""
    with SessionLocal() as db:
        db.add(RpAiMarketItem(
            id="manual-item", name="Pointer", item_type="tool", service_type="code",
            source="manual", repo_url="https://gitlab.example/someone/else",
            creator=OWNER, status="approved",
        ))
        db.commit()
    r = _new_version(client, "manual-item", version="1.1.0")
    assert r.status_code == 400
    assert "手工登记" in r.json()["detail"]
    # 最要紧的一条:没有替它建出一个新的 GitLab 项目。
    assert "created" not in gitlab_stubbed


# --- 改启动信息(不换内容) ------------------------------------------------


@pytest.fixture
def archive_stubbed(monkeypatch):
    """GitLab 归档打桩,形状照它自己的来:外面一层 `<项目>-<ref>-<sha>/`。"""
    def fake_archive(project_id, ref=None):
        return _zip({
            "revise-me-main-deadbeef/my-tool/README.md": "# my-tool\n",
            "revise-me-main-deadbeef/my-tool/main.py": "print(1)\n",
            f"revise-me-main-deadbeef/my-tool/{pkg.SIDECAR_NAME}":
                'name: "Revise Me"\nlaunch: "python main.py"\n',
        })
    monkeypatch.setattr(au, "fetch_repo_archive", fake_archive)


def _revise(client, item_id: str, *, eid: str = OWNER, version: str = "1.1.0", **over):
    data = {
        "eid": eid, "itemId": item_id, "version": version,
        "launch": "python main.py --fixed", "runtime": "long-running",
        "prerequisites": "先登出 SAP",
    }
    data.update(over)
    return client.post(
        "/api/market/agent-upload/metadata", data=data, headers={"X-User-Eid": eid}
    )


def test_revising_the_launch_info_adds_a_version_without_a_new_zip(
    client, gitlab_stubbed, archive_stubbed
) -> None:
    item_id = _first_upload(client)
    r = _revise(client, item_id)
    assert r.status_code == 201, r.text
    assert r.json()["version"] == "1.1.0"

    with SessionLocal() as db:
        item = db.get(RpAiMarketItem, item_id)
        assert item.version == "1.1.0"
        rows = list(db.scalars(
            select(RpAiMarketItemVersion)
            .where(RpAiMarketItemVersion.item_id == item_id)
            .order_by(RpAiMarketItemVersion.id.desc())
        ).all())
        assert len(rows) == 2
        newest, first = rows[0], rows[1]
        assert newest.metadata_json["launch"] == "python main.py --fixed"
        assert newest.metadata_json["runtime"] == "long-running"
        assert newest.metadata_json["prerequisites"] == ["先登出 SAP"]
        # 安装步骤是从包结构推出来的,内容没换就原样带过来,而不是变成空的。
        assert newest.metadata_json["install"] == (first.metadata_json or {}).get("install")
        assert newest.metadata_json["revisionOf"] == first.version
        # 交上来的还是上一版那个文件 —— 这一版不是"没有包"。
        assert newest.artifact_name == first.artifact_name
        assert newest.artifact_bytes == first.artifact_bytes


def test_revising_rewrites_the_sidecar_where_it_already_is(
    client, gitlab_stubbed, archive_stubbed
) -> None:
    """包自带一层顶层目录时 sidecar 在那一层里面。写到仓库根会留下两份。"""
    item_id = _first_upload(client)
    assert _revise(client, item_id).status_code == 201
    tree = gitlab_stubbed["tree"]
    assert f"my-tool/{pkg.SIDECAR_NAME}" in tree
    assert pkg.SIDECAR_NAME not in tree          # 根上没有第二份
    assert "--fixed" in tree[f"my-tool/{pkg.SIDECAR_NAME}"]
    # 归档外面那层 `<项目>-<ref>-<sha>/` 没被当成内容推回去。
    assert not any(p.startswith("revise-me-main-") for p in tree)


def test_only_the_uploader_may_revise_the_launch_info(
    client, gitlab_stubbed, archive_stubbed
) -> None:
    item_id = _first_upload(client)
    r = _revise(client, item_id, eid=OTHER)
    assert r.status_code == 403


def test_a_synced_item_has_no_launch_info_to_revise(
    client, gitlab_stubbed, archive_stubbed
) -> None:
    """同步进来的条目没有门户替它保管的包,它的安装说明本来就不来自这里。"""
    with SessionLocal() as db:
        db.add(RpAiMarketItem(
            id="gl-999001", name="Synced", item_type="tool", service_type="code",
            source="gitlab-tools", repo_url="https://gitlab.example/rp-ai/rp-ai-tool/x",
            creator=OWNER, status="approved",
        ))
        db.commit()
    r = _revise(client, "gl-999001")
    assert r.status_code == 400
    assert "不是上传来的包" in r.json()["detail"]


def test_a_revision_cannot_reuse_a_version_number(
    client, gitlab_stubbed, archive_stubbed
) -> None:
    item_id = _first_upload(client)
    r = _revise(client, item_id, version="1.0.0")
    assert r.status_code == 400
