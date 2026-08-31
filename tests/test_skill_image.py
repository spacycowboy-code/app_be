"""技能封面图:上传、返回、删除,以及围绕它的两条边界。

这一批盯住的是四件"错了不会报错"的事:

1. **存下来的必须是我们生成的字节**,不是用户发来的。所以有一条 polyglot 测试:一个合法 PNG
   后面挂一段脚本,存进去之后那段脚本必须不在了。仅靠"能打开"是验不出这一点的。
2. **没有主人的技能只有管理员能改**。官方导入的技能 ``uploader_eid`` 是空的,闸门在这里短路
   放行过一次(见 test_ownership_gates.py),所以这条要单独钉。
3. **改封面不能走内容那条链**。换一张图不该升版本、不该改 status/audit_state、不该通知收藏者。
   这条最容易在后来的重构里被"顺手复用 update_skill"破坏掉。
4. **换部门要换默认图**,但作者自己传的图不能被换组覆盖。
"""
from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import select

from app.database import SessionLocal
from app.models.category import RpAiCategory
from app.models.skill import RpAiSkill
from app.models.user import RpAiPortal
from app.services import skill_image

ADMIN = "ADMINMM"
OWNER = "EIMGOWN"
STRANGER = "EIMGSTR"
OWNED = "img-owned-skill"
ORPHAN = "img-orphan-skill"


def _png(size=(40, 30), color=(10, 200, 30, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(size=(40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 10, 10)).save(buf, format="JPEG")
    return buf.getvalue()


def _hdr(eid: str) -> dict:
    return {"X-User-Eid": eid}


@pytest.fixture(autouse=True)
def _rows():
    with SessionLocal() as db:
        for eid in (OWNER, STRANGER, ADMIN):
            if db.scalar(select(RpAiPortal).where(RpAiPortal.eid == eid)) is None:
                db.add(RpAiPortal(eid=eid))
        for cid, label in (("public", "Public"), ("radio-sw", "Radio SW")):
            if db.get(RpAiCategory, cid) is None:
                db.add(RpAiCategory(id=cid, label=label))
        # 先把被指向的行落进去。技能行有两个外键(uploader_eid -> rp_ai_portal.eid、
        # category_id -> rp_ai_category.id),同一批 insert 里 sqlite 会直接报
        # FOREIGN KEY constraint failed(conftest 打开了 PRAGMA foreign_keys)。
        db.flush()
        db.add(RpAiSkill(
            id=OWNED, name="Owned Skill", category_id="public", category="Public",
            image="skill-images/tavily.jpg", status="approved", uploader_eid=OWNER,
            version="1.0.0", audit_state="pass",
        ))
        db.add(RpAiSkill(
            id=ORPHAN, name="Orphan Skill", category_id="public", category="Public",
            image="skill-images/tavily.jpg", status="approved", uploader_eid=None,
        ))
        db.commit()
    yield
    with SessionLocal() as db:
        for sid in (OWNED, ORPHAN):
            row = db.get(RpAiSkill, sid)
            if row:
                db.delete(row)
        db.commit()


# --- normalize:存下来的是我们生成的字节 --------------------------------------


def test_a_png_survives_and_keeps_its_mime() -> None:
    data, mime = skill_image.normalize(_png())
    assert mime == "image/png"
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "PNG"


def test_the_format_is_not_silently_converted() -> None:
    """JPEG 存回 JPEG。统一转格式会把带透明通道的 PNG 压成白底,所以格式必须原样保留。"""
    _, mime = skill_image.normalize(_jpeg())
    assert mime == "image/jpeg"


def test_trailing_payload_is_dropped() -> None:
    """PNG 头 + 尾部夹带的脚本 —— 存下来之后夹带必须消失。

    这是重新编码而不是原样存字节的**唯一**理由。原样存的话这个文件在库里是"一张能打开的 PNG",
    而同源返回时那段尾部就是攻击面。
    """
    payload = b"<script>alert(1)</script>"
    data, _ = skill_image.normalize(_png() + payload)
    assert payload not in data


def test_exif_is_stripped() -> None:
    """EXIF 可能带 GPS 坐标,这是别人上传的照片里最不该被门户存下来的东西。"""
    src = io.BytesIO()
    img = Image.new("RGB", (40, 30), (1, 2, 3))
    exif = img.getexif()
    exif[0x010E] = "ImageDescription-marker"  # 一个好找的标记
    img.save(src, format="JPEG", exif=exif)
    assert b"ImageDescription-marker" in src.getvalue()

    data, _ = skill_image.normalize(src.getvalue())
    assert b"ImageDescription-marker" not in data


def test_oversized_image_is_scaled_down() -> None:
    data, _ = skill_image.normalize(_png(size=(2400, 1200)))
    with Image.open(io.BytesIO(data)) as img:
        assert max(img.size) <= skill_image.MAX_DIMENSION
        # 等比,不是拉伸。
        assert img.size[0] / img.size[1] == pytest.approx(2.0, abs=0.02)


def test_a_small_image_is_not_upscaled() -> None:
    data, _ = skill_image.normalize(_png(size=(24, 24)))
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (24, 24)


@pytest.mark.parametrize("raw", [b"", b"not an image at all", b"GIF89a" + b"\x00" * 40])
def test_non_images_and_unsupported_formats_are_refused(raw: bytes) -> None:
    with pytest.raises(skill_image.InvalidImage):
        skill_image.normalize(raw)


def test_svg_is_refused() -> None:
    """SVG 是可执行的 XML,同源返回等于存储型 XSS,而且没法靠重新编码消毒。"""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(skill_image.InvalidImage):
        skill_image.normalize(svg)


# --- 端点:上传 / 返回 / 删除 --------------------------------------------------


def test_owner_can_upload_and_then_fetch_the_image(client) -> None:
    r = client.put(
        f"/api/skills/{OWNED}/image",
        files={"file": ("cover.png", _png(), "image/png")},
        headers=_hdr(OWNER),
    )
    assert r.status_code == 200, r.text
    assert r.json()["hasCustomImage"] is True

    g = client.get(f"/api/skills/{OWNED}/image")
    assert g.status_code == 200
    assert g.headers["content-type"] == "image/png"
    assert g.headers["x-content-type-options"] == "nosniff"
    with Image.open(io.BytesIO(g.content)) as img:
        assert img.format == "PNG"


def test_no_custom_image_is_404_so_the_frontend_can_fall_back(client) -> None:
    assert client.get(f"/api/skills/{OWNED}/image").status_code == 404


def test_the_declared_content_type_is_not_trusted(client) -> None:
    """声明成 image/png 的一段文本必须被拒 —— 判定只看解码结果。"""
    r = client.put(
        f"/api/skills/{OWNED}/image",
        files={"file": ("evil.png", b"<html>not a png</html>", "image/png")},
        headers=_hdr(OWNER),
    )
    assert r.status_code == 400


def test_oversized_upload_is_refused_with_413(client) -> None:
    too_big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (skill_image.MAX_UPLOAD_BYTES + 1)
    r = client.put(
        f"/api/skills/{OWNED}/image",
        files={"file": ("big.png", too_big, "image/png")},
        headers=_hdr(OWNER),
    )
    assert r.status_code == 413


def test_etag_makes_a_repeat_fetch_a_304(client) -> None:
    client.put(
        f"/api/skills/{OWNED}/image",
        files={"file": ("cover.png", _png(), "image/png")},
        headers=_hdr(OWNER),
    )
    first = client.get(f"/api/skills/{OWNED}/image")
    again = client.get(
        f"/api/skills/{OWNED}/image", headers={"If-None-Match": first.headers["etag"]}
    )
    assert again.status_code == 304
    assert again.content == b""


def test_delete_falls_back_to_the_department_default(client) -> None:
    client.put(
        f"/api/skills/{OWNED}/image",
        files={"file": ("cover.png", _png(), "image/png")},
        headers=_hdr(OWNER),
    )
    r = client.delete(f"/api/skills/{OWNED}/image", headers=_hdr(OWNER))
    assert r.status_code == 200
    body = r.json()
    assert body["hasCustomImage"] is False
    assert body["image"] == skill_image.category_image("public")
    assert client.get(f"/api/skills/{OWNED}/image").status_code == 404


# --- 闸门 --------------------------------------------------------------------


def test_the_response_is_narrow_on_purpose(client) -> None:
    """封面图端点只返回 id / image / hasCustomImage。

    钉住这一点是因为改回 ``SkillRead`` 看着更"完整"却是个陷阱:写端点返回的是裸技能行,
    ``favoriteCount`` 是 0、``group`` 和 ``pendingUpdate`` 是空(它们由 get_skill 另外附上),
    前端拿它整体替换本地对象就会把收藏数冲成 0。
    """
    r = client.put(
        f"/api/skills/{OWNED}/image",
        files={"file": ("cover.png", _png(), "image/png")},
        headers=_hdr(OWNER),
    )
    assert set(r.json()) == {"id", "image", "hasCustomImage"}


def test_a_stranger_cannot_change_someone_elses_cover(client) -> None:
    r = client.put(
        f"/api/skills/{OWNED}/image",
        files={"file": ("cover.png", _png(), "image/png")},
        headers=_hdr(STRANGER),
    )
    assert r.status_code == 403


def test_a_stranger_cannot_change_a_cover_on_a_skill_with_no_uploader(client) -> None:
    """官方导入的技能没有上传者。闸门在这里**不能**短路放行。"""
    r = client.put(
        f"/api/skills/{ORPHAN}/image",
        files={"file": ("cover.png", _png(), "image/png")},
        headers=_hdr(STRANGER),
    )
    assert r.status_code == 403
    assert "管理员" in r.json()["detail"]


def test_admin_can_change_an_orphan_cover(client) -> None:
    r = client.put(
        f"/api/skills/{ORPHAN}/image",
        files={"file": ("cover.png", _png(), "image/png")},
        headers=_hdr(ADMIN),
    )
    assert r.status_code == 200


def test_a_stranger_cannot_delete_someone_elses_cover(client) -> None:
    assert client.delete(
        f"/api/skills/{OWNED}/image", headers=_hdr(STRANGER)
    ).status_code == 403


# --- 改封面不走内容那条链 ------------------------------------------------------


def test_changing_the_cover_does_not_touch_version_status_or_audit(client) -> None:
    """封面是展示元数据。它变了没有人需要重新安装,所以不该升版本、不该改审计状态。

    这条钉住的是"别人后来顺手把它改成复用 update_skill"——那条路会升版本、往 GitLab 提交、
    并通知所有收藏者"已更新到 vX"。
    """
    with SessionLocal() as db:
        before = db.get(RpAiSkill, OWNED)
        snapshot = (before.version, before.status, before.audit_state,
                    before.repo_commit, before.storage_path)

    client.put(
        f"/api/skills/{OWNED}/image",
        files={"file": ("cover.png", _png(), "image/png")},
        headers=_hdr(OWNER),
    )

    with SessionLocal() as db:
        after = db.get(RpAiSkill, OWNED)
        assert (after.version, after.status, after.audit_state,
                after.repo_commit, after.storage_path) == snapshot
        # 也不该长出一行版本历史。
        from app.models.version import RpAiSkillVersion
        assert db.scalars(
            select(RpAiSkillVersion).where(RpAiSkillVersion.skill_id == OWNED)
        ).all() == []


# --- 换部门要换默认图(此前的 bug) --------------------------------------------


def test_moving_to_another_group_refreshes_the_default_image(client, monkeypatch) -> None:
    """把技能挪到别的部门,卡片上的默认图要跟着变。

    此前 update_skill 只刷新了 denormalize 的 ``category`` 标签,默认图留在原部门那一张,而
    界面上没有任何地方能纠正它。
    """
    _stub_edit_path(monkeypatch)
    r = client.put(
        f"/api/skills/{OWNED}",
        json={"categoryId": "radio-sw"},
        headers=_hdr(OWNER),
    )
    assert r.status_code == 200, r.text
    assert r.json()["image"] == "skill-images/radio-sw.jpg"


def test_a_custom_cover_is_not_overwritten_by_a_group_change(client, monkeypatch) -> None:
    """作者传过自己的图,换部门不能把它悄悄换掉 —— 那是他的选择。"""
    client.put(
        f"/api/skills/{OWNED}/image",
        files={"file": ("cover.png", _png(), "image/png")},
        headers=_hdr(OWNER),
    )
    _stub_edit_path(monkeypatch)
    r = client.put(
        f"/api/skills/{OWNED}",
        json={"categoryId": "radio-sw"},
        headers=_hdr(OWNER),
    )
    assert r.status_code == 200, r.text
    assert r.json()["hasCustomImage"] is True
    # 自定义图还在,GET 仍然返回它。
    assert client.get(f"/api/skills/{OWNED}/image").status_code == 200


def _stub_edit_path(monkeypatch) -> None:
    """把 update_skill 里出网的三件事换掉:审计、仓库写入、以及正文读取。

    这条测试关心的只是"换部门之后 image 是什么",不该顺带去连审计引擎和 GitLab。
    """
    from types import SimpleNamespace

    from app.routers import skills as skills_router
    from app.services.audit_engine import AuditResult

    monkeypatch.setattr(
        skills_router, "audit_skill",
        lambda **kw: AuditResult(state="pass", findings=[], reason=None),
    )
    monkeypatch.setattr(
        skills_router, "_read_skill_md",
        lambda skill: f"---\nname: {skill.name}\ndescription: d\n---\nbody\n",
    )
    monkeypatch.setattr(
        skills_router.skill_storage, "store",
        lambda **kw: SimpleNamespace(
            version=kw.get("current_version") or "1.0.0",
            storage_path="public/x", commit_sha="deadbeef", tag="v1.0.0",
            artifact_name=None, pushed=True,
        ),
    )
