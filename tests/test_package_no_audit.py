"""tool / agent 的包上传不跑安全审计。

原来跑,而且 `/inspect` 做成事件流就是为了它 —— 审计要几十秒到几分钟。但对包它从来不改
流向(`blocking: False`):产出只有一份给审核人看的报告,而闸门是人工审核。所以它是纯成本,
上传要多等几十秒、还多一个必须活着的下游服务。

这里钉三件事,因为"少调用一个服务"这种改动最容易被后来的人无意中改回去:
1. `/inspect` 答的是普通 JSON,不是事件流(事件流的唯一理由就是等审计);
2. 谁都不许碰审计引擎 —— 用一个会让测试失败的桩当绊线;
3. 这个 router 的源码里不许再出现审计引擎(发布那条路也一样,不用把整套 GitLab 打桩)。

**skill 不在此列。** 它的审计**就是**闸门(`skill_audit.gate`:pass 才 approved,可疑进复核
分支),不是报告。那条路一个字都没动。
"""
from __future__ import annotations

import inspect as _inspect
import io
import zipfile

import pytest

from app.routers import agent_upload as au
from app.services import audit_engine

README = "# my-agent\n\nDoes a thing.\n"


def _zip(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in files.items():
            zf.writestr(path, text)
    return out.getvalue()


def _post(client, blob: bytes, item_type: str = "tool"):
    return client.post(
        "/api/market/agent-upload/inspect",
        data={"itemType": item_type},
        files={"file": ("my-agent.zip", blob, "application/zip")},
    )


def test_inspect_answers_plain_json_not_an_event_stream(client) -> None:
    """事件流的唯一理由是等审计。审计没了,一次答完 —— 界面也就不用那条阶段条了。"""
    r = _post(client, _zip({"my-agent/README.md": README, "my-agent/main.py": "print(1)\n"}))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    # 确认屏靠这份事实:文件清单和从包里推出来的启动命令/安装步骤。
    assert body["topDir"] == "my-agent"
    assert any(f["path"] == "my-agent/main.py" for f in body["files"])


def test_inspect_never_touches_the_audit_engine(client, monkeypatch) -> None:
    """绊线。用 pytest.fail 而不是 assert:它抛的是 BaseException 的子类,`except Exception`
    抓不到 —— 而这条路径上到处是兜底的 except。"""
    def refuse(*a, **kw):
        pytest.fail("包上传又去调审计引擎了。这条路不审计,见模块开头。")

    monkeypatch.setattr(audit_engine, "audit_skill", refuse)
    monkeypatch.setattr(au, "audit_skill", refuse, raising=False)
    r = _post(client, _zip({"my-agent/README.md": README}))
    assert r.status_code == 200


def test_a_package_with_plaintext_credentials_is_still_refused(client) -> None:
    """去掉的是审计,不是这个。明文凭据 GitLab 的 pre-receive 钩子必定拒收 —— 提前说,
    是为了让失败发生在第一秒而不是第两分钟、而且组里不留下一个空项目。"""
    blob = _zip({
        "my-agent/README.md": README,
        "my-agent/cfg.py": 'TOKEN = "glpat-abcdefghijklmnopqrst"\n',
    })
    r = _post(client, blob)
    assert r.status_code == 400
    assert "明文凭据" in r.json()["detail"]


def test_the_router_does_not_mention_the_audit_engine() -> None:
    """连发布那条路一起钉住,不用把整套 GitLab 打桩。

    只查名字而不是行为,是因为这里要防的不是某个 bug,而是"顺手把审计加回来" —— 而那一定
    会以这两个名字出现。
    """
    src = _inspect.getsource(au)
    assert "audit_skill" not in src
    assert "audit_engine" not in src
    # 历史版本行里那份留痕不动,所以列名还在模型上;但这个 router 不再写它。
    assert "audit_json=" not in src
