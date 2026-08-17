"""Turn one scan report into a market decision.

This is the only table we maintain, and it is where this platform's context enters. Its
rules were measured, not assumed: 46 real skills from the skill repo plus a written
malicious probe, and the OASB public benchmark's skill subset.

The gate acts on concealment and overreach, not on capability. A skill that declares it
uses the network and then uses the network is not suspicious; hiding what it does, or
doing more than it declared, is.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.skill_audit.scan import INCOMPLETE_RULES, ScanReport

_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# Facts about packaging, not about behaviour. They belong in the report the author reads;
# putting them in front of a reviewer spends someone's attention on a missing licence
# field. Measured: these accounted for 26 of 46 real skills having a finding at all.
HYGIENE_RULES = frozenset({
    "MANIFEST_MISSING_LICENSE",
    "MANIFEST_INVALID_NAME",
    "MANIFEST_DESCRIPTION_TOO_LONG",
    "ARCHIVE_FILE_DETECTED",
    "PYCACHE_FILES_DETECTED",
    "BINARY_FILE_DETECTED",
})

# "Something is deliberately hidden" is an assertion of concealment, not an observation
# of capability, so it gates one level lower than everything else. Measured: this took
# unicode-steganography detection on the public benchmark from 6-7/10 to 9/10 while
# adding no false review on 85 benign samples.
CONCEALMENT_CATEGORIES = frozenset({"obfuscation", "unicode_steganography"})

# A file the scanner could not read is only a reason for review when it is something that
# runs. Unreadable *data* (docx, xlsx, pdf, images) is normal in a skill bundle and was
# the single largest source of needless review before this distinction existed.
UNREADABLE_RULES = frozenset({"UNANALYZABLE_BINARY", "LOW_ANALYZABILITY"})
EXECUTABLE_SUFFIXES = (
    ".bat", ".cmd", ".ps1", ".sh", ".bash", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyz", ".pyo", ".jar", ".vbs", ".scr", ".msi", ".apk", ".com",
)

# Which side of the gate a finding came from. Refusal needs both, and this is structural:
# it does not depend on any category name staying stable.
_DETERMINISTIC = frozenset({
    "static", "static_analyzer", "yara", "pipeline", "bytecode", "behavioral",
    "behavioral_analyzer", "analyzability", "virustotal", "aidefense",
})


@dataclass
class Decision:
    state: str                     # pass | suspect | danger
    reason: str = ""               # one line, for the record and the reviewer
    incomplete: list[str] | None = None


def _is_executable(path: str | None) -> bool:
    return bool(path) and str(path).lower().rstrip("\"'").endswith(EXECUTABLE_SUFFIXES)


def _side(analyzer: str) -> str:
    return "deterministic" if analyzer in _DETERMINISTIC else "semantic"


def gating_findings(report: ScanReport) -> list:
    """The findings that may change the outcome, with the rest left in the report."""
    out = []
    for f in report.findings:
        if f.rule_id in HYGIENE_RULES or f.rule_id in INCOMPLETE_RULES:
            continue
        if f.rule_id in UNREADABLE_RULES:
            # Severity does not apply here. The reason is not "how bad is it" but
            # "we could not read something that runs", and the only way to resolve
            # that is a person reading it.
            if _is_executable(f.file):
                out.append(f)
            continue
        floor = "medium" if f.category in CONCEALMENT_CATEGORIES else "high"
        if _ORDER.get(f.severity, 0) >= _ORDER[floor]:
            out.append(f)
    return out


def decide(report: ScanReport, *, expected_analyzers: frozenset[str] | None = None) -> Decision:
    """Map a scan report to pass / suspect / danger.

    Order matters: "we did not finish looking" is answered before "we found nothing",
    because those are different statements and only one of them is evidence.
    """
    if not report.ran:
        return Decision("suspect", f"审计没有产出结果:{report.error or '未知原因'}")

    incomplete = report.incomplete
    missing = sorted(expected_analyzers - set(report.analyzers_used)) if expected_analyzers else []
    if incomplete or missing:
        detail = ", ".join(incomplete + [f"{m} 未运行" for m in missing])
        return Decision("suspect", f"某一层未跑完:{detail}", incomplete=incomplete + missing)

    gating = gating_findings(report)
    if not gating:
        return Decision("pass")

    # Refusal is the only irreversible outcome, so it needs two independent layers
    # pointing at the same thing. One layer's critical is a reason for a person to look,
    # not for the platform to refuse on its own.
    for f in (g for g in gating if g.severity == "critical"):
        if any(_side(g.source) != _side(f.source) and g.category == f.category
               for g in gating if g is not f):
            return Decision("danger", f"两层一致的 critical:{f.category}({f.rule_id})")

    unreadable = [f for f in gating if f.rule_id in UNREADABLE_RULES]
    if unreadable and len(unreadable) == len(gating):
        return Decision("suspect", f"读不了的可执行文件:{unreadable[0].file}")
    worst = max((f for f in gating if f.rule_id not in UNREADABLE_RULES),
                key=lambda f: _ORDER.get(f.severity, 0), default=gating[0])
    return Decision("suspect", f"{worst.severity} {worst.category}({worst.rule_id})")
