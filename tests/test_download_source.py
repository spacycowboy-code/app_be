"""下载接口的字节来自技能那一行记着的引用。

改之前这个接口没有测试(codegraph:`download_skill` — no covering tests found),而它有三层回落,
最后一层是**拿 `rp_ai_skill.instructions` 合成一个只有 SKILL.md 的 zip**。那个包看不出自己缺了
脚本和附件,用户会以为拿到了完整的技能 —— 所以这里要的是明确失败,不是凑一个包。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.database import SessionLocal
from app.models.skill import RpAiSkill
from app.repositories import skill_content

REF = "d" * 40
PATH = "team/download-probe_EXXX_Download_Probe"


def _seed(skill_id: str, **over) -> None:
    fields = dict(
        id=skill_id,
        name="Download Probe",
        category_id="public",
        status="approved",
        version="1.0.0",
        tags=[],
        storage_path=PATH,
        repo_commit=REF,
        instructions="# 数据库里那份副本\n",
    )
    fields.update(over)
    with SessionLocal() as db:
        db.add(RpAiSkill(**fields))
        db.commit()


def _drop(skill_id: str) -> None:
    with SessionLocal() as db:
        row = db.get(RpAiSkill, skill_id)
        if row:
            db.delete(row)
            db.commit()


@pytest.fixture
def probe():
    _seed("download-probe")
    yield "download-probe"
    _drop("download-probe")


def _author_zip() -> bytes:
    """作者上传的原始包:自带一层同名外壳,下载时要剥掉,不能变成 <名>/<名>/SKILL.md。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Download Probe/SKILL.md", b"# author copy")
        zf.writestr("Download Probe/scripts/run.py", b"print(1)")
    return buf.getvalue()


def _names(resp) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        return sorted(n for n in zf.namelist() if not n.endswith("/"))


def test_the_authors_original_package_wins_and_is_not_double_nested(
    client, probe, monkeypatch
):
    tree = {
        "SKILL.md": b"# extracted definition only",
        "manifest.json": b"{}",
        "artifacts/orig.zip": _author_zip(),
    }
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: tree)
    resp = client.get(f"/api/skills/{probe}/download")
    assert resp.status_code == 200
    assert _names(resp) == ["Download_Probe/SKILL.md", "Download_Probe/scripts/run.py"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.read("Download_Probe/SKILL.md") == b"# author copy"


def test_without_an_artifact_the_loose_files_ship_without_marketplace_extras(
    client, probe, monkeypatch
):
    tree = {
        "SKILL.md": b"# md",
        "scripts/run.py": b"print(1)",
        "manifest.json": b"{}",
        "artifacts/stale.txt": b"not a zip",
    }
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: tree)
    resp = client.get(f"/api/skills/{probe}/download")
    assert resp.status_code == 200
    assert _names(resp) == ["Download_Probe/SKILL.md", "Download_Probe/scripts/run.py"]


def test_unreadable_content_is_503_not_a_zip_built_from_the_database_copy(
    client, probe, monkeypatch
):
    def boom(ref: str, prefix: str):
        raise skill_content.ContentUnavailable("URLError")

    monkeypatch.setattr(skill_content, "read_tree", boom)
    resp = client.get(f"/api/skills/{probe}/download")
    assert resp.status_code == 503
    assert b"PK\x03\x04" not in resp.content, "读不到内容却还是给了一个 zip"


def test_an_empty_version_is_404_rather_than_a_synthesized_skill_md(
    client, probe, monkeypatch
):
    monkeypatch.setattr(skill_content, "read_tree", lambda ref, prefix: {})
    resp = client.get(f"/api/skills/{probe}/download")
    assert resp.status_code == 404
    assert b"PK\x03\x04" not in resp.content


def test_a_skill_without_a_ref_is_409_and_never_reads_content(client, monkeypatch):
    _seed("no-ref-download", storage_path=None, repo_commit=None)
    monkeypatch.setattr(
        skill_content, "read_tree",
        lambda *a: pytest.fail("没有引用时不该去读内容存储"),
    )
    try:
        resp = client.get("/api/skills/no-ref-download/download")
        assert resp.status_code == 409
    finally:
        _drop("no-ref-download")
