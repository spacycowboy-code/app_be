"""Coverage and cause survive to the record, because that is what makes them answerable.

``reason`` is a sentence, and part of it comes from the scanner, whose wording is not ours. Two
questions are asked of the record afterwards and neither can be answered from prose: "was this
content examined at all" and "how often does the audit itself fail, and on what".

Older records have both columns NULL, meaning "not recorded then". They are never backfilled by
re-reading the prose — that would replace a fact with today's guess.
"""
import pytest

from app.database import SessionLocal
from app.models.audit import RpAiSkillAuditRecord
from app.services.skill_audit import gate, verdict
from app.services.skill_audit.scan import ScanReport
from app.services.skill_audit.types import AuditResult, Finding

ALL_CAUSES = sorted({
    verdict.CAUSE_BUNDLE_UNLOADABLE, verdict.CAUSE_BUNDLE_UNREADABLE_MEMBER,
    verdict.CAUSE_BUNDLE_TOO_BIG, verdict.CAUSE_SCANNER_TIMEOUT,
    verdict.CAUSE_SCANNER_BROKEN, verdict.CAUSE_LAYER_FAILED,
})
ALL_COVERAGE = [verdict.COVERAGE_COMPLETE, verdict.COVERAGE_PARTIAL, verdict.COVERAGE_NONE]


def _record(ref: str, **kw) -> None:
    with SessionLocal() as db:
        gate.record(db, ref, AuditResult(**kw), skill_id=None, skill_name="s",
                    uploader_eid="EYINCNG")
        db.commit()


def _read(ref: str) -> RpAiSkillAuditRecord:
    with SessionLocal() as db:
        rec = db.get(RpAiSkillAuditRecord, ref)
        assert rec is not None
        return rec


class TestBothFieldsArePersisted:
    @pytest.mark.parametrize("coverage", ALL_COVERAGE)
    def test_every_coverage_round_trips(self, coverage):
        _record(f"cov-{coverage}", state="suspect", reason="r", coverage=coverage)
        assert _read(f"cov-{coverage}").coverage == coverage

    @pytest.mark.parametrize("cause", ALL_CAUSES)
    def test_every_cause_round_trips(self, cause):
        _record(f"cau-{cause}", state="suspect", reason="r",
                coverage=verdict.COVERAGE_PARTIAL, cause=cause)
        assert _read(f"cau-{cause}").cause == cause

    def test_the_columns_are_wide_enough(self):
        """A silently truncated value would corrupt exactly the counts it exists for."""
        assert max(len(c) for c in ALL_COVERAGE) <= RpAiSkillAuditRecord.coverage.type.length
        assert max(len(c) for c in ALL_CAUSES) <= RpAiSkillAuditRecord.cause.type.length

    def test_complete_coverage_stores_no_cause(self):
        _record("no-cause", state="pass", coverage=verdict.COVERAGE_COMPLETE)
        rec = _read("no-cause")
        assert rec.coverage == verdict.COVERAGE_COMPLETE
        assert rec.cause is None


class TestTheQuestionsTheRecordHasToAnswer:
    def test_platform_failures_can_be_separated_from_the_authors_mistakes(self):
        _record("q1", state="suspect", reason="r", coverage=verdict.COVERAGE_NONE,
                cause=verdict.CAUSE_SCANNER_TIMEOUT)
        _record("q2", state="suspect", reason="r", coverage=verdict.COVERAGE_PARTIAL,
                cause=verdict.CAUSE_LAYER_FAILED)
        _record("q3", state="suspect", reason="r", coverage=verdict.COVERAGE_NONE,
                cause=verdict.CAUSE_BUNDLE_UNLOADABLE)
        platform = [c for c in ALL_CAUSES if verdict.owner_of(c) == verdict.OWNER_PLATFORM]
        with SessionLocal() as db:
            rows = db.query(RpAiSkillAuditRecord).filter(
                RpAiSkillAuditRecord.cause.in_(platform)).all()
        assert {r.id for r in rows} == {"q1", "q2"}

    def test_unexamined_content_can_be_found(self):
        _record("e1", state="suspect", reason="r", coverage=verdict.COVERAGE_NONE,
                cause=verdict.CAUSE_BUNDLE_UNLOADABLE)
        _record("e2", state="suspect", reason="r", coverage=verdict.COVERAGE_PARTIAL,
                cause=verdict.CAUSE_BUNDLE_UNREADABLE_MEMBER)
        _record("e3", state="pass", coverage=verdict.COVERAGE_COMPLETE)
        with SessionLocal() as db:
            none_rows = db.query(RpAiSkillAuditRecord).filter(
                RpAiSkillAuditRecord.coverage == verdict.COVERAGE_NONE).all()
        assert {r.id for r in none_rows} == {"e1"}, (
            "partial coverage is not the same question: something was examined there"
        )

    def test_an_old_record_is_left_alone(self):
        """NULL means "not recorded then". Nothing may turn it into a value by guessing."""
        _record("legacy", state="suspect", reason="审计没有产出结果:timed out after 900s",
                coverage="", cause=None)
        rec = _read("legacy")
        assert rec.coverage is None and rec.cause is None
        assert rec.reason, "the raw sentence is kept, so a later reclassification has the fact"


class TestTheValuesComeFromTheVerdict:
    def test_the_seam_carries_what_decide_chose(self):
        d = verdict.decide(ScanReport(ran=False, error="timed out after 900s"))
        _record("seam", state=d.state, reason=d.reason, coverage=d.coverage, cause=d.cause)
        rec = _read("seam")
        assert rec.coverage == verdict.COVERAGE_NONE
        assert rec.cause == verdict.CAUSE_SCANNER_TIMEOUT

    def test_a_finding_is_not_recorded_as_an_audit_failure(self):
        d = verdict.decide(ScanReport(
            ran=True, analyzers_used=["static_analyzer"],
            findings=[Finding(source="static", category="command_injection",
                              severity="high", rule_id="X")]))
        assert d.coverage == verdict.COVERAGE_COMPLETE and d.cause is None
