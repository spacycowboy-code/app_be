"""四处所有权闸门:查不到主人时不许放行。

四处都是同一个写法 —— `if not is_admin and X.owner and requester != X.owner: 403` —— 而它在
`owner` 为空时**短路放行**。没有主人的东西不是公共的:

- 市场里同步进来的条目 `creator` 是 NULL,而它们的 id 是可以推出来的 `gl-<GitLab 项目 id>`;
- 官方导入的技能 `uploader_eid` 是空的。

于是"任何登录用户都能删掉它们"、"都能把一个条目改指向自己的仓库"、"都能给它推一版新内容而
页面上作者一个字不变"。最后这个尤其不是编辑,是内容替换。

包上传改成传完即上市之后这几处更要紧:入口不再有闸门,能不能改动一个已经上市的条目就成了
唯一的边界。查不到主人时该做的是交给管理员,不是对所有人开放。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.market import RpAiMarketItem
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal

ADMIN = "ADMINMM"
OWNER = "EOWNER1"
STRANGER = "ESTRNG1"
ORPHAN_ID = "gl-141618"          # 同步进来的条目就是这个形状,creator 为 NULL
OWNED_ID = "gl-141619"


@pytest.fixture(autouse=True)
def _rows():
    with SessionLocal() as db:
        for eid in (OWNER, STRANGER, ADMIN):
            if db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid)) is None:
                db.add(RpAiPortal(eid=eid))
        for item_id, creator in ((ORPHAN_ID, None), (OWNED_ID, OWNER)):
            db.add(RpAiMarketItem(
                id=item_id, name=f"Item {item_id}", item_type="tool", service_type="code",
                source="gitlab-agents" if creator is None else "upload",
                repo_url="https://gitlab.example/rp-ai/rp-ai-agent/x",
                category_id="public", category="Public", creator=creator,
                version="1.0.0", status="approved", tags=[],
            ))
        db.add(RpAiSkill(
            id="official-skill", name="Official Skill", category_id="public",
            category="Public", status="approved", uploader_eid=None,
        ))
        db.commit()
    yield


def _hdr(eid: str) -> dict:
    return {"X-User-Eid": eid}


# --- 市场条目:删 ------------------------------------------------------------


def test_a_stranger_cannot_delete_an_item_with_no_owner(client) -> None:
    r = client.delete(f"/api/market/{ORPHAN_ID}", headers=_hdr(STRANGER))
    assert r.status_code == 403
    assert "管理员" in r.json()["detail"]
    with SessionLocal() as db:
        assert db.get(RpAiMarketItem, ORPHAN_ID) is not None


def test_a_stranger_cannot_delete_someone_elses_item(client) -> None:
    assert client.delete(f"/api/market/{OWNED_ID}", headers=_hdr(STRANGER)).status_code == 403


def test_the_owner_and_the_admin_can_still_delete(client) -> None:
    """闸门收紧了,但两个该能做的人还得能做 —— 传完即上市之后作者必须能自己撤回。"""
    assert client.delete(f"/api/market/{OWNED_ID}", headers=_hdr(OWNER)).status_code == 204
    assert client.delete(f"/api/market/{ORPHAN_ID}", headers=_hdr(ADMIN)).status_code == 204


# --- 市场条目:改 ------------------------------------------------------------


def _edit(client, item_id: str, eid: str, repo: str):
    return client.put(
        f"/api/market/{item_id}",
        json={
            "name": "Item", "itemType": "tool", "serviceType": "code",
            "repoUrl": repo, "categoryId": "public",
        },
        headers=_hdr(eid),
    )


def test_a_stranger_cannot_repoint_an_item_with_no_owner(client) -> None:
    """这一处最容易被当成小事:改的是 repo_url,而卡片、名字、下载入口一个字不变。"""
    r = _edit(client, ORPHAN_ID, STRANGER, "https://gitlab.example/mine/evil")
    assert r.status_code == 403
    with SessionLocal() as db:
        assert "evil" not in (db.get(RpAiMarketItem, ORPHAN_ID).repo_url or "")


def test_the_admin_can_still_fix_an_item_with_no_owner(client) -> None:
    r = _edit(client, ORPHAN_ID, ADMIN, "https://gitlab.example/rp-ai/rp-ai-agent/x")
    assert r.status_code == 200


# --- 技能:下架 --------------------------------------------------------------


def test_a_stranger_cannot_unpublish_an_official_skill(client) -> None:
    r = client.delete("/api/skills/official-skill", headers=_hdr(STRANGER))
    assert r.status_code == 403
    assert "管理员" in r.json()["detail"]


# --- 包:推新版本 ------------------------------------------------------------


def test_a_stranger_cannot_push_a_new_version_onto_an_ownerless_item(client) -> None:
    """走的是发布端点,所以在建项目、推送之前就要被挡下来 —— GitLab 一侧不需要打桩。"""
    r = client.post(
        "/api/market/agent-upload",
        data={
            "eid": STRANGER, "name": "Item", "categoryId": "public",
            "launch": "python main.py", "runtime": "oneshot",
            "itemType": "tool", "itemId": ORPHAN_ID,
        },
        files={"file": ("pkg.zip", _minimal_zip(), "application/zip")},
        headers=_hdr(STRANGER),
    )
    assert r.status_code == 403
    assert "上传者" in r.json()["detail"]


def _minimal_zip() -> bytes:
    import io
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("t/README.md", "# t\n")
        zf.writestr("t/main.py", "print(1)\n")
    return out.getvalue()
