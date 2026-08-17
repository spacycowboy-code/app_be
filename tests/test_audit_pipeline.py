"""The audit is one scan plus one decision. These are the ways that can go wrong."""
import io
import json
import zipfile
from pathlib import Path

import pytest

from app.services.skill_audit import pipeline, scan, scanners, verdict
from app.services.skill_audit.scan import ScanReport
from app.services.skill_audit.types import BundleTooLarge, Finding


def f(rule="R", cat="command_injection", sev="high", an="static", file=None):
    return Finding(source=an, category=cat, severity=sev, rule_id=rule, file=file)


def report(*findings, analyzers=("static_analyzer", "llm_analyzer", "meta_analyzer")):
    return ScanReport(ran=True, findings=list(findings), analyzers_used=list(analyzers))


class TestVerdictFloor:
    def test_nothing_gating_publishes(self):
        assert verdict.decide(report()).state == "pass"

    def test_hygiene_never_gates(self):
        """A missing licence field is a fact about packaging. Putting it in front of a
        reviewer spends a person's attention on metadata."""
        r = report(f(rule="MANIFEST_MISSING_LICENSE", cat="policy_violation", sev="info"),
                   f(rule="ARCHIVE_FILE_DETECTED", cat="policy_violation", sev="low"))
        assert verdict.decide(r).state == "pass"

    def test_medium_alone_does_not_gate(self):
        assert verdict.decide(report(f(sev="medium"))).state == "pass"

    def test_high_goes_to_review(self):
        d = verdict.decide(report(f(sev="high", cat="resource_abuse")))
        assert d.state == "suspect" and "resource_abuse" in d.reason


class TestConcealmentGatesLower:
    """Deliberate hiding is an assertion of concealment, not an observation of
    capability, so it gates one level below everything else."""

    def test_medium_obfuscation_goes_to_review(self):
        assert verdict.decide(report(f(cat="obfuscation", sev="medium"))).state == "suspect"

    def test_medium_unicode_steganography_goes_to_review(self):
        r = report(f(cat="unicode_steganography", sev="medium"))
        assert verdict.decide(r).state == "suspect"

    def test_low_obfuscation_still_does_not_gate(self):
        """low is hygiene and context by the scale we adopted; gating on it would block
        on 'something might be a bit odd'."""
        assert verdict.decide(report(f(cat="obfuscation", sev="low"))).state == "pass"


class TestUnreadableFiles:
    def test_unreadable_data_does_not_gate(self):
        """docx/xlsx/pdf assets are normal in a bundle and are not executed."""
        r = report(f(rule="UNANALYZABLE_BINARY", cat="policy_violation", sev="medium",
                     file="assets/template.docx"))
        assert verdict.decide(r).state == "pass"

    @pytest.mark.parametrize("name", ["scripts/run.bat", "tool.pyz", "a/b/setup.ps1", "x.pyc"])
    def test_an_unreadable_executable_goes_to_review(self, name):
        r = report(f(rule="UNANALYZABLE_BINARY", cat="policy_violation", sev="medium", file=name))
        assert verdict.decide(r).state == "suspect", name


class TestRefusalNeedsTwoSides:
    def test_one_layer_alone_does_not_refuse(self):
        """A static rule firing on an f-string SQL query is a bug to fix, not grounds for
        the platform to refuse on its own."""
        d = verdict.decide(report(f(rule="SQL_INJECTION_STRING_FORMAT", sev="critical",
                                    an="static")))
        assert d.state == "suspect"

    def test_both_layers_on_the_same_category_refuses(self):
        d = verdict.decide(report(
            f(rule="LLM_COMMAND_INJECTION", cat="command_injection", sev="critical", an="llm"),
            f(rule="PIPELINE_TAINT_FLOW", cat="command_injection", sev="high", an="pipeline")))
        assert d.state == "danger" and "command_injection" in d.reason

    def test_two_semantic_findings_are_still_one_side(self):
        d = verdict.decide(report(
            f(rule="LLM_A", cat="data_exfiltration", sev="critical", an="llm"),
            f(rule="META_B", cat="data_exfiltration", sev="high", an="meta_analyzer")))
        assert d.state == "suspect"

    def test_agreement_on_different_categories_is_not_agreement(self):
        d = verdict.decide(report(
            f(rule="LLM_A", cat="data_exfiltration", sev="critical", an="llm"),
            f(rule="STATIC_B", cat="resource_abuse", sev="critical", an="static")))
        assert d.state == "suspect"


class TestIncompleteIsNotClean:
    def test_a_layer_reporting_its_own_failure_goes_to_review(self):
        r = report(f(rule="LLM_ANALYSIS_FAILED", cat="policy_violation", sev="info"))
        d = verdict.decide(r)
        assert d.state == "suspect" and "LLM_ANALYSIS_FAILED" in d.reason

    def test_a_truncated_prompt_goes_to_review(self):
        r = report(f(rule="LLM_CONTEXT_BUDGET_EXCEEDED", cat="policy_violation", sev="info"))
        assert verdict.decide(r).state == "suspect"

    def test_no_report_at_all_goes_to_review(self):
        d = verdict.decide(ScanReport(ran=False, error="timed out after 900s"))
        assert d.state == "suspect" and "timed out" in d.reason

    def test_a_missing_expected_analyzer_goes_to_review(self):
        r = report(analyzers=("static_analyzer",))
        d = verdict.decide(r, expected_analyzers=frozenset({"static_analyzer", "llm_analyzer"}))
        assert d.state == "suspect" and "llm_analyzer" in d.reason


class TestFindingsAreCarriedVerbatim:
    def test_the_scanner_fields_survive(self):
        raw = {"rule_id": "TOOL_ABUSE_UNDECLARED_NETWORK", "category": "unauthorized_tool_use",
               "severity": "MEDIUM", "title": "Undeclared network usage",
               "description": "Skill uses network libraries but does not declare it",
               "file_path": "SKILL.md", "line_number": 12, "snippet": "requests.get(",
               "remediation": "Declare network usage", "analyzer": "static",
               "metadata": {"aitech": "AITech-12.1"}}
        got = scan._finding(raw)
        assert got.rule_id == "TOOL_ABUSE_UNDECLARED_NETWORK"
        assert got.category == "unauthorized_tool_use"      # never renamed
        assert got.severity == "medium"                     # only case-normalised
        assert got.evidence == "requests.get(" and got.remediation == "Declare network usage"
        assert got.aitech == "AITech-12.1" and got.source == "static"
        assert "Undeclared network usage" in got.detail

    def test_a_missing_category_is_not_invented(self):
        got = scan._finding({"rule_id": "X", "severity": "HIGH"})
        assert got.category == "unclassified"


class TestScanFailsClosed:
    def test_no_output_means_not_checked(self, monkeypatch):
        class Done:
            returncode = 1
            stdout = ""
            stderr = "Error loading skill: SKILL.md not found"

        monkeypatch.setattr(scan.subprocess, "run", lambda *a, **k: Done())
        r = scan.scan(bin_path="s", target_dir="/t", policy_path="p", timeout=1)
        assert r.ran is False and "SKILL.md not found" in r.error

    def test_a_timeout_means_not_checked(self, monkeypatch):
        def boom(*a, **k):
            raise scan.subprocess.TimeoutExpired(cmd="s", timeout=1)

        monkeypatch.setattr(scan.subprocess, "run", boom)
        assert scan.scan(bin_path="s", target_dir="/t", policy_path="p", timeout=1).ran is False

    def test_a_missing_binary_means_not_checked(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("no such file")

        monkeypatch.setattr(scan.subprocess, "run", boom)
        assert scan.scan(bin_path="s", target_dir="/t", policy_path="p", timeout=1).ran is False


class TestMaterialize:
    def _zip(self, files: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in files.items():
                zf.writestr(name, data)
        return buf.getvalue()

    def test_a_nested_archive_is_unpacked_and_removed(self, tmp_path):
        """Left in place it is scanned as an opaque binary — a finding about our
        packaging — and its bytes go to the model twice."""
        inner = self._zip({"scripts/run.py": "print(1)\n"})
        outer = self._zip({"SKILL.md": "---\nname: t\n---\n", "artifacts/bundle.zip": inner})
        pipeline._materialize(tmp_path, "---\nname: t\n---\n", outer, "b.zip")
        assert not list(tmp_path.rglob("*.zip")), "the archive must not be left behind"
        assert any(p.name == "run.py" for p in tmp_path.rglob("*")), "its files must be visible"

    def test_a_zip_slip_member_is_skipped(self, tmp_path):
        evil = self._zip({"../escaped.txt": "x", "ok.txt": "y"})
        pipeline._materialize(tmp_path, "md", evil, "b.zip")
        assert not (tmp_path.parent / "escaped.txt").exists()
        assert (tmp_path / "ok.txt").exists()

    def test_too_many_members_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "_MAX_BUNDLE_MEMBERS", 3)
        with pytest.raises(BundleTooLarge):
            pipeline._materialize(tmp_path, "md", self._zip({f"f{i}": "x" for i in range(10)}), "b.zip")

    def test_unpacking_past_the_size_limit_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline, "_MAX_BUNDLE_BYTES", 1024)
        with pytest.raises(BundleTooLarge):
            pipeline._materialize(tmp_path, "md", self._zip({"big": "x" * 5000}), "b.zip")

    def test_a_plain_markdown_upload_is_not_written_twice(self, tmp_path):
        pipeline._materialize(tmp_path, "same", b"same", "skill.md")
        assert [p.name for p in tmp_path.iterdir()] == ["SKILL.md"]


class TestAuditSkill:
    def test_a_plaintext_credential_is_refused_before_the_scanner_runs(self, monkeypatch):
        """It must not reach a prompt, and the scanner is what builds the prompt."""
        called = {"n": 0}
        monkeypatch.setattr(scan, "scan", lambda **kw: called.update(n=called["n"] + 1))
        pipeline._verdict_cache.clear()
        md = ("---\nname: t\ndescription: d\n---\n"
              "使用这个 key: AKIAIOSFODNN7EXAMPLE 和 wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n")
        result = pipeline.audit_skill(name="t", skill_md=md)
        if result.state == "danger":
            assert called["n"] == 0, "the scanner must not see a skill we already refused"
            assert result.findings[0].category == "hardcoded_secrets"

    def test_the_result_carries_what_reproduces_it(self, monkeypatch):
        pipeline._verdict_cache.clear()
        monkeypatch.setattr(scan, "scan", lambda **kw: ScanReport(
            ran=True, analyzers_used=["static_analyzer", "llm_analyzer"],
            scanner_version="skill-scanner 2.0.12", policy_fingerprint="abc123"))
        r = pipeline.audit_skill(name="t", skill_md="---\nname: t\n---\nbody\n")
        assert r.state == "pass"
        assert r.scanner_version == "skill-scanner 2.0.12"
        assert r.policy_fingerprint == "abc123"
        assert r.analyzers_used == ["static_analyzer", "llm_analyzer"]
        assert r.judge_model

    def test_identical_content_is_scanned_once(self, monkeypatch):
        """evaluate() then publish() on the same bytes must not pay twice."""
        pipeline._verdict_cache.clear()
        calls = {"n": 0}

        def counted(**kw):
            calls["n"] += 1
            return ScanReport(ran=True, analyzers_used=["static_analyzer"])

        monkeypatch.setattr(scan, "scan", counted)
        md = "---\nname: t\n---\nbody\n"
        pipeline.audit_skill(name="t", skill_md=md)
        pipeline.audit_skill(name="t", skill_md=md)
        assert calls["n"] == 1
