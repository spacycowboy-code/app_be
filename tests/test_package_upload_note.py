"""上传一个包时可以附一条说明,可选。

**为什么在版本行上而不是在条目上。** 条目上的 `description` 答「这东西是什么」,下一版把它
改掉是对的;而「这一版改了什么」是关于那一版的事实,写到条目上会被下一版盖掉,于是版本历史
里除了最新那行以外全都说不出自己那次的事。列(`rp_ai_market_item_version.notes`)自建表起
就在,一直没有写入者 —— 这里给它一个。

这一组钉四件容易悄悄错的事:

1. **不填是合法的。** 这是需求的原话,所以它是第一条:没有它,这个功能会变成又一格必填。
2. **空白与没写是同一个值。** 只填空格存下来是空字符串,页面上那一格既不像「写了空的」也
   不像「没写」,而两者本来就是一件事。
3. **超长是拒而不是截断。** 悄悄截掉一半等于把作者没打算发的说明发出去。而且要在**收包之前**
   拒 —— 在最后一米拒的话,GitLab 项目里已经多了一个提交。
4. **两个入口都收。** 换内容(`/agent-upload`)和改包信息(`/agent-upload/metadata`)都产生
   一行版本行,只在一边收会让版本历史里那一列有一半的行永远是空的。
"""
from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.market_version import RpAiMarketItemVersion
from app.routers import agent_upload as au
from app.services import gitlab_projects as glw

OWNER = "EZHGMIO"
PROJECT_ID = 626262
ITEM_ID = f"gl-{PROJECT_ID}"


def _zip(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in files.items():
            zf.writestr(path, text)
    return out.getvalue()


@pytest.fixture
def gitlab_stubbed(monkeypatch):
    """GitLab 那一侧打桩,记下推了几次 —— 第 3 条要断言\"一次都没推\"。"""
    pushed: dict = {"pushes": 0}

    def fake_target(item_type: str) -> glw.UploadTarget:
        return glw.UploadTarget(
            item_type=item_type, group_path=f"rp-ai/rp-ai-user-{item_type}s",
            token="stub", env_var="STUB_TOKEN",
        )

    def fake_create(*, slug, name, description=None, target):
        return glw.ProjectRef(
            id=PROJECT_ID,
            path_with_namespace=f"{target.group_path}/{slug}",
            web_url=f"https://gitlab.example/{target.group_path}/{slug}",
            http_url=f"https://gitlab.example/{target.group_path}/{slug}.git",
        )

    def fake_publish(project, src, *, version, message, tmp_root, target):
        pushed["pushes"] += 1
        return "c" * 40, f"v{version}"

    monkeypatch.setattr(glw, "target_for", fake_target)
    monkeypatch.setattr(glw, "signing_args", lambda: [])
    monkeypatch.setattr(glw, "create_project", fake_create)
    monkeypatch.setattr(glw, "publish_tree", fake_publish)
    return pushed


def _upload(client, *, notes: str | None = None, version: str | None = None):
    data = {
        "eid": OWNER, "name": "Note Me", "categoryId": "public",
        "launch": "python main.py", "runtime": "oneshot", "itemType": "tool",
    }
    if notes is not None:
        data["notes"] = notes
    if version is not None:
        data["itemId"] = ITEM_ID
        data["version"] = version
    return client.post(
        "/api/market/agent-upload",
        data=data,
        files={"file": ("pkg.zip", _zip({
            "my-tool/README.md": "# my-tool\n",
            "my-tool/main.py": "print(1)\n",
        }), "application/zip")},
        headers={"X-User-Eid": OWNER},
    )


def _notes_by_version() -> dict[str, str | None]:
    with SessionLocal() as db:
        return {
            v.version: v.notes
            for v in db.scalars(
                select(RpAiMarketItemVersion).where(
                    RpAiMarketItemVersion.item_id == ITEM_ID
                )
            ).all()
        }


def test_a_note_is_stored_on_the_version_row(client, gitlab_stubbed) -> None:
    assert _upload(client, notes="第一版,只支持 EEF 报告").status_code == 201
    assert _notes_by_version() == {"1.0.0": "第一版,只支持 EEF 报告"}


def test_leaving_it_out_is_fine(client, gitlab_stubbed) -> None:
    """需求的原话:不强制填写。整个字段不出现在表单里也要能传。"""
    assert _upload(client).status_code == 201
    assert _notes_by_version() == {"1.0.0": None}


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_is_the_same_as_not_written(client, gitlab_stubbed, blank) -> None:
    """存成空字符串会在版本历史里留一个说不清是哪种情况的空格子。"""
    assert _upload(client, notes=blank).status_code == 201
    assert _notes_by_version() == {"1.0.0": None}


def test_surrounding_whitespace_is_trimmed(client, gitlab_stubbed) -> None:
    assert _upload(client, notes="  修了时区  \n").status_code == 201
    assert _notes_by_version() == {"1.0.0": "修了时区"}


def test_a_too_long_note_is_refused_before_the_package_is_pushed(
    client, gitlab_stubbed
) -> None:
    """拒,不截断 —— 而且要在推 GitLab 之前拒,否则项目里已经多了一个提交。"""
    resp = _upload(client, notes="х" * (au._NOTE_MAX + 1))
    assert resp.status_code == 400, resp.text
    assert str(au._NOTE_MAX) in resp.text
    assert gitlab_stubbed["pushes"] == 0
    assert _notes_by_version() == {}


def test_each_version_keeps_its_own_note(client, gitlab_stubbed) -> None:
    """下一版的说明不覆盖上一版的 —— 这是「存在版本行上」这个决定的全部意义。"""
    assert _upload(client, notes="第一版").status_code == 201
    assert _upload(client, notes="修了时区", version="1.0.1").status_code == 201
    assert _notes_by_version() == {"1.0.0": "第一版", "1.0.1": "修了时区"}


def test_a_later_version_without_a_note_does_not_erase_the_earlier_one(
    client, gitlab_stubbed
) -> None:
    assert _upload(client, notes="第一版").status_code == 201
    assert _upload(client, version="1.0.1").status_code == 201
    assert _notes_by_version() == {"1.0.0": "第一版", "1.0.1": None}


def test_the_versions_endpoint_returns_it(client, gitlab_stubbed) -> None:
    """详情页那张版本历史表读的是这个端点 —— 存住了但返回不出来等于没做。"""
    _upload(client, notes="第一版")
    rows = client.get(f"/api/market/{ITEM_ID}/versions").json()
    assert [r["notes"] for r in rows] == ["第一版"]


# --- 边界:只有包上传那条路收说明 --------------------------------------------
#
# 「改包信息」(`/metadata`)不换包,所以不收。它产生的版本行在「说明」那一列永远是空的 ——
# 这是定下来的口径,所以要有测试钉住,否则下一个人看到那一列有一半是空的会当成漏了去补上。


@pytest.fixture
def archive_stubbed(monkeypatch):
    """GitLab 归档打桩,形状照它自己的来:外面一层 `<项目>-<ref>-<sha>/`。"""
    def fake_archive(project_id, ref=None):
        return _zip({
            "note-me-main-deadbeef/my-tool/README.md": "# my-tool\n",
            "note-me-main-deadbeef/my-tool/main.py": "print(1)\n",
        })

    monkeypatch.setattr(au, "fetch_repo_archive", fake_archive)


def _revise(client, *, version: str = "1.1.0", notes: str | None = None):
    data = {
        "eid": OWNER, "itemId": ITEM_ID, "version": version,
        "launch": "python main.py --fixed", "runtime": "oneshot",
    }
    if notes is not None:
        data["notes"] = notes
    return client.post(
        "/api/market/agent-upload/metadata", data=data, headers={"X-User-Eid": OWNER}
    )


def test_revising_the_package_info_leaves_the_note_empty(
    client, gitlab_stubbed, archive_stubbed
) -> None:
    """改包信息那条路不收说明 —— 连送上来也不存。

    只断言「没填时是空的」不够:那和「收了但这次没填」分不开。所以这里**故意送一个** ——
    接口是公开的,一个被忽略的字段和一个被存下的字段在响应上长得一样。
    """
    assert _upload(client, notes="第一版").status_code == 201
    assert _revise(client, notes="这一条不该被存下来").status_code == 201
    assert _notes_by_version() == {"1.0.0": "第一版", "1.1.0": None}


def test_revising_does_not_carry_the_previous_note_forward(
    client, gitlab_stubbed, archive_stubbed
) -> None:
    """也不继承上一版的说明。

    这条路的其他字段刻意从上一版**带过来**(artifact_name / artifact_bytes / install),
    所以「说明也一起带」是这里最可能出的错,而它的后果是一句描述别的版本的话挂在新版本上。
    """
    assert _upload(client, notes="第一版").status_code == 201
    assert _revise(client).status_code == 201
    assert _notes_by_version() == {"1.0.0": "第一版", "1.1.0": None}
