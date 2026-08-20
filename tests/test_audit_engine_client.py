"""门户怎么跟审计引擎说话。

引擎在另一个仓,所以这里不测"审计判得对不对",只测**门户这一侧的行为**,而且每条都对着一个真实后果:

* 引擎连不上、返回错的契约版本、流没给结论 —— 这三件事结果必须一样:一个说明"没能查"的结论,
  而且 `retryable`,好让 `gate` 拒绝改动已上线内容。
* 附件过大是**作者**能改的,不能混进上面那一类,否则上传者只看到"审计失败"。
* 阶段事件要一字不差地转给上传界面,`ping` 不能露出去。
* 分类表取不到时要**说自己算不出来**,不能返回一个偏小的数字 —— 那种数字看起来像"平台很稳定",
  恰好在链路有问题时最容易误导人。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from app.services import audit_engine
from app.services.audit_engine import BundleTooLarge, audit_skill


class _Stream:
    """假的事件流响应:按行吐字节,可当上下文管理器。"""

    def __init__(self, *events: tuple[str, dict]):
        self._lines = []
        for name, payload in events:
            self._lines.append(f"event: {name}\n".encode())
            self._lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}\n".encode())
            self._lines.append(b"\n")

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _result(**over) -> dict:
    d = {"apiVersion": "v1", "state": "pass", "coverage": "complete", "findings": []}
    d.update(over)
    return d


@pytest.fixture()
def engine(monkeypatch):
    """把 urlopen 换掉,并记录请求。"""
    seen: dict = {}

    def install(outcome):
        def fake(req, timeout=None):
            seen["req"] = req
            seen["timeout"] = timeout
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return seen

    return install


# --------------------------------------------------------------------- 拿不到结论

def test_a_dead_engine_becomes_a_result_that_says_so(engine):
    engine(OSError("connection refused"))
    r = audit_skill(name="s", skill_md="# s\n")
    assert r.state == "suspect", "不是 danger:我们并没有发现问题,只是没能查"
    assert r.coverage == audit_engine.COVERAGE_NONE
    assert r.cause == audit_engine.CAUSE_ENGINE_UNREACHABLE
    assert r.retryable is True, "平台侧的缺口,gate 要靠它保护已上线内容"


def test_a_rejected_request_is_not_mistaken_for_a_clean_audit(engine):
    engine(urllib.error.HTTPError("u", 401, "unauthorized", {}, None))
    r = audit_skill(name="s", skill_md="# s\n")
    assert r.state == "suspect" and r.retryable is True
    assert r.coverage == audit_engine.COVERAGE_NONE


def test_a_contract_mismatch_stops_instead_of_guessing(engine):
    """版本不认识就当不可用,不要猜字段。

    猜的后果是把一个读不懂的响应当成审计结论用,而它可能恰好缺了 state 或 coverage。
    """
    engine(_Stream(("result", _result(apiVersion="v2"))))
    r = audit_skill(name="s", skill_md="# s\n")
    assert r.cause == audit_engine.CAUSE_ENGINE_CONTRACT
    assert r.retryable is True


def test_a_result_without_state_is_not_a_result(engine):
    engine(_Stream(("result", {"apiVersion": "v1", "coverage": "complete"})))
    r = audit_skill(name="s", skill_md="# s\n")
    assert r.cause == audit_engine.CAUSE_ENGINE_CONTRACT


def test_a_stream_that_just_ends_is_not_a_pass(engine):
    """流结束了却没有终结事件 —— 最危险的一种,因为它最像成功。"""
    engine(_Stream(("stage", {"stage": "deterministic"})))
    r = audit_skill(name="s", skill_md="# s\n")
    assert r.state == "suspect" and r.coverage == audit_engine.COVERAGE_NONE


def test_an_engine_error_event_is_a_platform_problem(engine):
    engine(_Stream(("error", {"detail": "扫描器不见了", "cause": "engine_failed"})))
    r = audit_skill(name="s", skill_md="# s\n")
    assert r.retryable is True and "扫描器不见了" in r.reason


# --------------------------------------------------------------------- 作者的问题

def test_too_big_a_bundle_is_raised_for_the_author_not_swallowed(engine):
    """附件过大不能变成"审计失败":作者只需要把附件改小,而 422 会把这句话送到他眼前。"""
    engine(_Stream(("error", {"detail": "附件解包后 60MB,超过预算", "cause": "bundle_too_big"})))
    with pytest.raises(BundleTooLarge, match="60MB"):
        audit_skill(name="s", skill_md="# s\n")


# --------------------------------------------------------------------- 正常路径

def test_stages_reach_the_caller_and_pings_do_not(engine):
    engine(_Stream(("stage", {"stage": "deterministic"}),
                   ("ping", {}),
                   ("stage", {"stage": "semantic"}),
                   ("ping", {}),
                   ("result", _result(state="pass"))))
    seen: list[str] = []
    r = audit_skill(name="s", skill_md="# s\n", on_progress=seen.append)
    assert seen == ["deterministic", "semantic"], "ping 不该露给上传界面"
    assert r.state == "pass" and r.retryable is False


def test_unknown_fields_are_ignored_but_known_ones_arrive(engine):
    engine(_Stream(("result", _result(
        state="suspect", coverage="partial", cause="layer_failed", retryable=True,
        reason="某一层没跑完", incomplete=["LLM_ANALYSIS_FAILED"],
        analyzers_used=["static_analyzer"], scanner_version="skill-scanner 2.0.12",
        findings=[{"source": "static", "category": "hardcoded_secrets", "severity": "high",
                   "evidence": "AKIA…", "rule_id": "SECRET_AWS", "remediation": "轮换它",
                   "somethingTheEngineAddedLater": 1}],
        alsoNew={"a": 1}))))
    r = audit_skill(name="s", skill_md="# s\n")
    assert r.coverage == "partial" and r.cause == "layer_failed" and r.retryable is True
    assert r.incomplete == ["LLM_ANALYSIS_FAILED"]
    assert r.scanner_version == "skill-scanner 2.0.12"
    assert len(r.findings) == 1 and r.findings[0].remediation == "轮换它"


def test_the_read_timeout_is_the_ping_budget_not_the_audit_budget(engine):
    """读超时是"多久没有任何字节",不是"审计允许跑多久"。

    引擎每 15 秒一个 ping,所以一分钟量级足够;而一次审计本身可以十几分钟。把这两个混起来的后果是
    每个慢一点的审计都被当成引擎挂了。
    """
    seen = engine(_Stream(("result", _result())))
    audit_skill(name="s", skill_md="# s\n")
    assert seen["timeout"] == 60
    assert seen["req"].get_header("Accept") == "text/event-stream"
    assert seen["req"].get_header("Authorization", "").startswith("Bearer")


def test_the_artifact_goes_up_as_a_file_part(engine):
    seen = engine(_Stream(("result", _result())))
    audit_skill(name="s", skill_md="# s\n", artifact_bytes=b"PK\x03\x04zip",
                artifact_filename='we"ird\nname.zip')
    body = seen["req"].data
    assert b"PK\x03\x04zip" in body
    assert b'filename="' in body
    assert b"\nname.zip" not in body, "文件名里的换行会破坏 multipart 分隔"


# --------------------------------------------------------------------- 去重键

def test_the_content_key_is_ours_and_needs_no_engine():
    a = audit_engine.content_key("n", "# md\n", None)
    assert a == audit_engine.content_key("n", "# md\n", None)
    assert a != audit_engine.content_key("n", "# md changed\n", None)
    assert a != audit_engine.content_key("n2", "# md\n", None)
    assert a != audit_engine.content_key("n", "# md\n", b"artifact")


# --------------------------------------------------------------------- 分类表

def test_the_owner_is_rendered_from_the_engines_table():
    assert audit_engine.cause_owner("scanner_timeout") == "平台"
    assert audit_engine.cause_owner("bundle_unloadable") == "作者"
    assert audit_engine.cause_owner("bundle_too_big") == "作者与平台"
    assert audit_engine.cause_owner(None) is None
    assert audit_engine.cause_owner("something_new_the_engine_added") is None


def test_without_the_table_it_says_it_cannot_tell(monkeypatch):
    """取不到分类表时返回 None,让上层显示"算不出来"。

    返回 0 才是最坏的:那个数字看起来像"平台一次失败都没有",而它恰好出现在链路有问题的时候。
    """
    monkeypatch.setattr(audit_engine, "_vocab", None)
    monkeypatch.setattr(audit_engine, "_vocab_at", 0.0)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no route")))
    assert audit_engine.cause_owner("scanner_timeout") is None
    assert audit_engine.platform_owned(["scanner_timeout", "bundle_unloadable"]) is None


def test_with_the_table_platform_failures_are_counted():
    assert audit_engine.platform_owned(
        ["scanner_timeout", "scanner_broken", "bundle_unloadable"]) == 2


def test_the_portals_own_causes_have_an_owner_without_asking_the_engine(monkeypatch):
    """引擎连不上时,归属不能是"算不出来"。

    这两个 cause 是门户自己造的:引擎没应答,或者应答读不懂。查引擎的表永远查不到它们,而"引擎连不上"
    恰好是引擎故障时最常出现的那一条 —— 显示成"没有对应说明"的话,复核人看到的信息比我们知道的更少。
    这里连分类表都取不到,归属仍然要能给出来。
    """
    monkeypatch.setattr(audit_engine, "_vocab", None)
    monkeypatch.setattr(audit_engine, "_vocab_at", 0.0)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no route")))
    assert audit_engine.cause_owner(audit_engine.CAUSE_ENGINE_UNREACHABLE) == "平台"
    assert audit_engine.cause_owner(audit_engine.CAUSE_ENGINE_CONTRACT) == "平台"
    # 引擎那边的 cause 仍然是"算不出来",不能因为加了本地表就开始猜
    assert audit_engine.cause_owner("scanner_timeout") is None
