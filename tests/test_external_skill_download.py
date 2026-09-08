"""外部托管的技能:界面拿一个真链接去上游取,而不是让 XHR 跟着跳转。

**为什么需要这条路。** 从 IVC 的 Nexus 同步进来的技能门户手里没有字节,只有一个上游地址,
所以 `download_skill` 对它们答 307 转出去。这在浏览器里走不通:前端下载按钮走的是 XHR
(`responseType: 'blob'`),XHR 跟着跨域跳转时浏览器要求新域名回 `Access-Control-Allow-Origin`
—— Nexus 不回,请求在协议层被掐掉、连状态码都没有。用户看到「Network error / Failed to fetch」,
而服务端日志里是干干净净的 307。2026-09-01 生产上 17 条 `nexus-ivc` 技能全是这个症状。

所以这里钉住三件事:

1. 端点交出地址并**记一次**下载 —— 界面用真链接跳转之后门户什么都看不到了,计数只能记在跳之前。
2. 对**不是**外部托管的技能拒收。否则它就是一个不用真的下载任何东西就能刷下载量的入口。
3. `externallyHosted` / `externalUrl` 要出现在读模型里 —— 前端就是靠它决定渲染按钮还是链接;
   漏了这个字段,界面只能一视同仁地渲染成按钮,于是又回到那个解释不了的报错。
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.download import RpAiSkillDownload
from app.models.skill import RpAiSkill

NEXUS_URL = (
    "https://arm1s10-eiffel239.eiffel.gic.ericsson.se:8443/nexus/content/"
    "repositories/releases/skills/rvc-usage/0.0.10/rvc-usage-0.0.10.tar.gz"
)
OWNER = "EZHGMIO"


def _seed(skill_id: str, **over) -> None:
    fields = dict(
        id=skill_id,
        name="RVC Usage",
        category_id="public",
        status="approved",
        version="0.0.10",
        tags=[],
        downloads=0,
    )
    fields.update(over)
    with SessionLocal() as db:
        db.add(RpAiSkill(**fields))
        db.commit()


def _drop(*skill_ids: str) -> None:
    """删干净,**包括下载记录**。

    这不是洁癖:`_bump_downloads` 会往 `rp_ai_skill_download` 插一行,而那张表被报表类测试
    (以及月报本身)按人按部门统计。只删技能行会留下一条孤儿下载记录 —— 实测整套跑下来会让
    别的测试文件里 33 个用例失败、133 个 setup 报错,而单独跑这个文件是绿的。
    """
    with SessionLocal() as db:
        for sid in skill_ids:
            db.query(RpAiSkillDownload).filter(
                RpAiSkillDownload.skill_id == sid
            ).delete(synchronize_session=False)
            row = db.get(RpAiSkill, sid)
            if row:
                db.delete(row)
        db.commit()


@pytest.fixture
def hosted_elsewhere():
    """一条同步来的技能:有上游地址,没有 storage_path / repo_commit —— 生产上就是这个形状。"""
    _seed("ivc-rvc-usage", external_url=NEXUS_URL, external_source="nexus-ivc")
    yield "ivc-rvc-usage"
    _drop("ivc-rvc-usage")


@pytest.fixture
def held_here():
    _seed("local-probe", storage_path="team/local-probe", repo_commit="d" * 40)
    yield "local-probe"
    _drop("local-probe")


def test_the_read_model_says_the_content_lives_elsewhere(client, hosted_elsewhere) -> None:
    """前端分岔的唯一依据。漏这几个字段,界面就只能把它当普通技能渲染。"""
    r = client.get(f"/api/skills/{hosted_elsewhere}", headers={"X-User-Eid": OWNER})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["externallyHosted"] is True
    assert body["externalUrl"] == NEXUS_URL
    assert body["externalSource"] == "nexus-ivc"


def test_a_normal_skill_is_not_marked_as_hosted_elsewhere(client, held_here) -> None:
    """对照:门户自己存内容的技能这两格必须是空的,否则界面会把它们也变成外链。

    走列表而不是详情:详情会去内容仓读 SKILL.md,而这条探针行的 storage_path 是编的 ——
    那次失败是测试数据的问题,和这里要证的事无关。
    """
    r = client.get("/api/skills", headers={"X-User-Eid": OWNER})
    assert r.status_code == 200, r.text
    row = next((s for s in r.json() if s["id"] == held_here), None)
    assert row is not None
    assert row["externallyHosted"] is False
    assert row["externalUrl"] is None


def test_the_market_list_carries_it_too(client, hosted_elsewhere) -> None:
    """卡片上也有下载按钮,所以列表那个瘦形状也得带上,否则从卡片点还是那个报错。"""
    r = client.get("/api/skills", headers={"X-User-Eid": OWNER})
    assert r.status_code == 200, r.text
    row = next((s for s in r.json() if s["id"] == hosted_elsewhere), None)
    assert row is not None, "同步来的技能应该出现在市场列表里"
    assert row["externallyHosted"] is True
    assert row["externalUrl"] == NEXUS_URL


def test_going_upstream_hands_back_the_url_and_counts_once(client, hosted_elsewhere) -> None:
    r = client.post(
        f"/api/skills/{hosted_elsewhere}/external-download",
        headers={"X-User-Eid": OWNER},
    )
    assert r.status_code == 200, r.text
    assert r.json()["url"] == NEXUS_URL
    assert r.json()["source"] == "nexus-ivc"
    with SessionLocal() as db:
        assert db.get(RpAiSkill, hosted_elsewhere).downloads == 1


def test_a_skill_we_hold_ourselves_is_refused(client, held_here) -> None:
    """否则这是一个谁都能调、不用下载任何东西就能把下载量刷上去的入口。"""
    r = client.post(
        f"/api/skills/{held_here}/external-download", headers={"X-User-Eid": OWNER}
    )
    assert r.status_code == 409
    with SessionLocal() as db:
        assert db.get(RpAiSkill, held_here).downloads == 0


def test_the_redirect_route_still_works_for_clients_that_follow_it(
    client, hosted_elsewhere
) -> None:
    """307 那条分支保留:curl / CLI 会跟跳转,只有浏览器里的 XHR 用不了它。"""
    r = client.get(
        f"/api/skills/{hosted_elsewhere}/download",
        headers={"X-User-Eid": OWNER},
        follow_redirects=False,
    )
    assert r.status_code == 307
    assert r.headers["location"] == NEXUS_URL


def test_an_unknown_skill_is_404_not_409(client) -> None:
    r = client.post(
        "/api/skills/does-not-exist/external-download", headers={"X-User-Eid": OWNER}
    )
    assert r.status_code == 404
