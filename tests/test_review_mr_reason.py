"""The reason has to reach the two people who need it.

A verdict of "suspect" does not always come with findings. The commonest reason is that a
layer did not finish — the scanner refusing to load a malformed SKILL.md, for instance — and
that produces none. Rendering only the findings table left the review MR as a bare table
header: the reviewer could not tell why the skill was in front of them, and the author was
told nothing they could act on.

Seen in production on 2026-08-19: the same skill uploaded twice, refused both times for
``SKILL.md missing required field: name``, and the review MR merged five minutes later with no
evidence in it at all.
"""
import re

import pytest

from app.services import gitlab_mr
from app.services import audit_engine
from app.services.audit_engine import Finding

REASON_NO_REPORT = "审计没有产出结果:Error loading skill: SKILL.md missing required field: name"


def f(rule="X", cat="prompt_injection", sev="high", file="SKILL.md", line=7,
      evidence="忽略以上所有指令", detail="推翻既有规则"):
    return Finding(source="llm", category=cat, severity=sev, rule_id=rule,
                   file=file, line=line, evidence=evidence, detail=detail)


class TestReviewBodyWithoutFindings:
    """This is the production case: suspect, zero findings."""

    def body(self, **kw):
        return gitlab_mr._findings_markdown(
            kw.get("name", "A Replir Allocation Tool"), kw.get("findings", []),
            kw.get("uploader", "Tony Li W (ERTABDF)"), kw.get("reason", REASON_NO_REPORT),
            kw.get("coverage", audit_engine.COVERAGE_NONE),
            kw.get("cause", "bundle_unloadable"))

    def test_the_reason_is_in_the_body(self):
        assert "SKILL.md missing required field: name" in self.body()

    def test_it_does_not_render_an_empty_table(self):
        body = self.body()
        assert "| 位置 | 检查项" not in body, "an empty table header is what reviewers saw"

    def test_it_says_the_content_was_never_examined(self):
        """The distinction that decides whether merging is safe at all."""
        body = self.body()
        assert "一点都没有被检查过" in body

    def test_it_names_the_actual_mistake_not_just_the_symptom(self):
        """The production case: the author *did* write `name:` — with no space after the colon.

        `name:foo` is a plain scalar in YAML, not a mapping, so the field reads as missing
        while looking present. Told only "a required field is missing", the author goes
        looking for something that is already there — which is why this skill was uploaded
        twice, unchanged.
        """
        body = self.body()
        assert "冒号后面少一个空格" in body, body
        assert "`name: 名字`" in body, "the correct form has to be shown, not just described"

    def test_the_uploader_is_still_named(self):
        assert "Tony Li W (ERTABDF)" in self.body()

    def test_merging_still_means_publishing(self):
        assert "合入此 MR = 该 skill 上线" in self.body()


class TestCoverageAndCause:
    """Driven by the two fields the verdict layer decided, not by grepping its prose.

    An earlier version matched substrings of the reason text. That recovered structure the
    verdict layer already had, and it was wrong twice over: it broke when the scanner reworded a
    message, and one of its markers ("Invalid frontmatter") turned out never to have existed — a
    test caught the invention only because it asserted against the real scanner.
    """

    ALL_CAUSES = sorted({
        "bundle_unloadable", "bundle_unreadable_member",
        "bundle_too_big", "scanner_timeout",
        "scanner_broken", "layer_failed",
    })

    @pytest.mark.parametrize("cause", ALL_CAUSES)
    def test_every_cause_has_an_owner_and_advice(self, cause):
        """The rule for adding a cause: it arrives with both, or this fails."""
        assert audit_engine.cause_owner(cause), cause
        body = gitlab_mr._findings_markdown("s", [], "u", "某个理由",
                                            audit_engine.COVERAGE_PARTIAL, cause)
        assert f"**归属:** {audit_engine.cause_owner(cause)}" in body, body
        assert "某个理由" in body, "the raw reason still has to be quoted"

    def test_complete_coverage_adds_no_note(self):
        body = gitlab_mr._findings_markdown("s", [f()], "u", "high prompt_injection(X)",
                                            audit_engine.COVERAGE_COMPLETE, None)
        assert "覆盖度" not in body
        assert "| 位置 |" in body

    def test_none_and_partial_say_different_things(self):
        none = gitlab_mr._findings_markdown("s", [], "u", "r", audit_engine.COVERAGE_NONE,
                                            "bundle_unloadable")
        partial = gitlab_mr._findings_markdown("s", [f()], "u", "r", audit_engine.COVERAGE_PARTIAL,
                                               "layer_failed")
        assert "一点都没有被检查过" in none
        assert "只检查了一部分" in partial

    def test_a_partial_verdict_still_shows_its_findings(self):
        """Partial coverage and findings are not exclusive: the table is still evidence."""
        body = gitlab_mr._findings_markdown("s", [f()], "u", "r", audit_engine.COVERAGE_PARTIAL,
                                            "bundle_unreadable_member")
        assert "只检查了一部分" in body
        assert "| 位置 |" in body

    def test_platform_causes_do_not_blame_the_author(self):
        for cause in ("scanner_timeout", "scanner_broken",
                      "layer_failed"):
            body = gitlab_mr._findings_markdown("s", [], "u", "r", audit_engine.COVERAGE_NONE, cause)
            assert audit_engine.cause_owner(cause) == "平台"
            assert "请作者" not in body, cause

    def test_an_unknown_cause_says_so_instead_of_guessing(self):
        body = gitlab_mr._findings_markdown("s", [], "u", "审计没有产出结果:something new",
                                            audit_engine.COVERAGE_NONE, "cause_we_never_defined")
        assert "归属" not in body
        assert "还没有对应的处置说明" in body
        assert "something new" in body

    # 原来这里还有一个 @pytest.mark.parametrize 块和四条测试,走的是 verdict.decide(ScanReport(...)):
    # 扫描器的各种错误文本分别该归到哪个 cause、某一层没跑完是 partial 而不是 none、可读性问题不算
    # "什么都没查"。那些测的是引擎怎么**决定**这两个字段,不是门户怎么**渲染**它们,已随拆分搬到
    # skill-audit 仓 tests/test_verdict_mapping.py。这里留下的都是渲染:同样的 coverage/cause 传进去,
    # 复核人应该读到什么。

class TestReviewBodyWithFindings:
    def body(self, reason="high prompt_injection(X)"):
        return gitlab_mr._findings_markdown("s", [f()], "Terry Yin (EYINCNG)", reason)

    def test_the_table_is_still_rendered(self):
        body = self.body()
        assert "| 位置 | 检查项 | 严重度 | 命中原文 | 说明 |" in body
        assert "SKILL.md:7" in body
        assert "忽略以上所有指令" in body

    def test_the_reason_is_shown_alongside_the_table(self):
        assert "high prompt_injection(X)" in self.body()

    def test_a_missing_reason_is_simply_omitted(self):
        """Older records have no reason; the body must not print an empty label."""
        body = gitlab_mr._findings_markdown("s", [f()], "u", None)
        assert "**判定理由:**" not in body
        assert "| 位置 |" in body

    def test_pipes_in_evidence_do_not_break_the_table(self):
        body = gitlab_mr._findings_markdown(
            "s", [f(evidence="a | b", detail="c | d")], "u", "r")
        rows = [ln for ln in body.splitlines() if ln.startswith("| `")]
        assert len(rows) == 1
        # Count only unescaped pipes: an escaped one is content, not a column break.
        columns = re.split(r"(?<!\\)\|", rows[0])
        assert len(columns) == 7, columns   # leading + 5 cells + trailing
        assert "a \\| b" in rows[0]


class TestReviewSuspectPassesTheReason:
    def test_the_reason_reaches_the_description_and_the_note(self, monkeypatch):
        sent = []

        def fake_request(method, path, payload=None):
            sent.append((method, path, payload))
            if path.endswith("merge_requests"):
                return {"web_url": "https://gitlab/mr/1", "iid": 1}
            return {}

        monkeypatch.setattr(gitlab_mr, "_request", fake_request)
        monkeypatch.setattr(gitlab_mr, "_reviewer_id", lambda _u: None)
        settings = gitlab_mr.get_settings()
        monkeypatch.setattr(settings, "audit_review_enabled", True, raising=False)

        url = gitlab_mr.review_suspect(
            source_branch="b", target_branch="main", skill_name="s",
            findings=[], audit_ref="ref1", uploader="Tony Li W (ERTABDF)",
            reason=REASON_NO_REPORT, coverage=audit_engine.COVERAGE_NONE,
            cause="bundle_unloadable",
        )
        assert url == "https://gitlab/mr/1"
        created = next(p for m, path, p in sent if path.endswith("merge_requests"))
        assert REASON_NO_REPORT in created["description"]
        note = next(p for m, path, p in sent if path.endswith("/notes"))
        assert REASON_NO_REPORT in note["body"], "the Discussion tab needs it too"

    def test_a_failure_to_open_the_mr_is_not_fatal(self, monkeypatch):
        """Unchanged behaviour, asserted because the reason plumbing runs through here."""
        def boom(*_a, **_k):
            raise RuntimeError("gitlab down")

        monkeypatch.setattr(gitlab_mr, "_request", boom)
        settings = gitlab_mr.get_settings()
        monkeypatch.setattr(settings, "audit_review_enabled", True, raising=False)
        assert gitlab_mr.review_suspect(
            source_branch="b", target_branch="main", skill_name="s", findings=[],
            audit_ref="r", reason="x") is None
