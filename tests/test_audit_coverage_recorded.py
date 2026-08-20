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
from app.services import audit_engine
from app.services.skill_audit import gate
from app.services.audit_engine import AuditResult, Finding

ALL_CAUSES = sorted({
    "bundle_unloadable", "bundle_unreadable_member",
    "bundle_too_big", "scanner_timeout",
    "scanner_broken", "layer_failed",
})
ALL_COVERAGE = [audit_engine.COVERAGE_COMPLETE, audit_engine.COVERAGE_PARTIAL, audit_engine.COVERAGE_NONE]


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
                coverage=audit_engine.COVERAGE_PARTIAL, cause=cause)
        assert _read(f"cau-{cause}").cause == cause

    def test_the_columns_are_wide_enough(self):
        """A silently truncated value would corrupt exactly the counts it exists for."""
        assert max(len(c) for c in ALL_COVERAGE) <= RpAiSkillAuditRecord.coverage.type.length
        assert max(len(c) for c in ALL_CAUSES) <= RpAiSkillAuditRecord.cause.type.length

    def test_complete_coverage_stores_no_cause(self):
        _record("no-cause", state="pass", coverage=audit_engine.COVERAGE_COMPLETE)
        rec = _read("no-cause")
        assert rec.coverage == audit_engine.COVERAGE_COMPLETE
        assert rec.cause is None


class TestTheQuestionsTheRecordHasToAnswer:
    def test_platform_failures_can_be_separated_from_the_authors_mistakes(self):
        _record("q1", state="suspect", reason="r", coverage=audit_engine.COVERAGE_NONE,
                cause="scanner_timeout")
        _record("q2", state="suspect", reason="r", coverage=audit_engine.COVERAGE_PARTIAL,
                cause="layer_failed")
        _record("q3", state="suspect", reason="r", coverage=audit_engine.COVERAGE_NONE,
                cause="bundle_unloadable")
        platform = [c for c in ALL_CAUSES if audit_engine.cause_owner(c) == "平台"]
        with SessionLocal() as db:
            rows = db.query(RpAiSkillAuditRecord).filter(
                RpAiSkillAuditRecord.cause.in_(platform)).all()
        assert {r.id for r in rows} == {"q1", "q2"}

    def test_unexamined_content_can_be_found(self):
        _record("e1", state="suspect", reason="r", coverage=audit_engine.COVERAGE_NONE,
                cause="bundle_unloadable")
        _record("e2", state="suspect", reason="r", coverage=audit_engine.COVERAGE_PARTIAL,
                cause="bundle_unreadable_member")
        _record("e3", state="pass", coverage=audit_engine.COVERAGE_COMPLETE)
        with SessionLocal() as db:
            none_rows = db.query(RpAiSkillAuditRecord).filter(
                RpAiSkillAuditRecord.coverage == audit_engine.COVERAGE_NONE).all()
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

# 原来这里还有一个类,两条测试走的是 verdict.decide(ScanReport(...)):"decide 选的 coverage/cause 会被
# 如实记下来"和"一条发现不会被记成审计失败"。它们测的是引擎怎么判,不是门户怎么记,已随拆分搬到
# skill-audit 仓。
