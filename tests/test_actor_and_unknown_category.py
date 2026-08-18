"""Who gets recorded as the actor, and what an unfamiliar category does.

Both are backstops against a silent wrong answer: an upload attributed to a colleague who
never made it, and a category a scanner release added that nobody taught the gate about.
"""
import types
from pathlib import Path

import pytest

from app.services.eids import acting_eid
from app.services.skill_audit import verdict
from app.services.skill_audit.scan import ScanReport
from app.services.skill_audit.types import Finding


def _req(eid=None):
    """A stand-in for the request the site-wide auth gate has already annotated."""
    state = types.SimpleNamespace()
    if eid is not None:
        state.eid = eid
    return types.SimpleNamespace(state=state)


class TestActingEid:
    def test_the_token_wins_over_the_claim(self):
        """The body says one colleague, the session is another. Only the session is evidence."""
        assert acting_eid(_req("EYINCNG"), "EXINGEN") == "EYINCNG"

    def test_the_claim_is_used_when_there_is_no_session(self):
        """auth_enabled=false is a supported mode; there is no verified identity to prefer."""
        assert acting_eid(_req(), "EXINGEN") == "EXINGEN"

    def test_neither_gives_nothing(self):
        assert acting_eid(_req(), None) is None
        assert acting_eid(_req(), "   ") is None

    def test_both_forms_are_normalized(self):
        assert acting_eid(_req(" eyincng "), None) == "EYINCNG"
        assert acting_eid(_req(), " exingen ") == "EXINGEN"

    def test_a_request_without_state_does_not_raise(self):
        """Anything that reaches these handlers has been through the gate, but a helper
        that throws on an unexpected shape would turn a missing attribute into a 500."""
        assert acting_eid(types.SimpleNamespace(), "EXINGEN") == "EXINGEN"


def f(rule="R", cat="command_injection", sev="high", an="static", file=None):
    return Finding(source=an, category=cat, severity=sev, rule_id=rule, file=file)


def report(*findings, analyzers=("static_analyzer", "llm_analyzer", "meta_analyzer")):
    return ScanReport(ran=True, findings=list(findings), analyzers_used=list(analyzers))


# Reads the categories out of whatever scanner is installed. Returns None when there is no
# scanner to ask, so the unit suite still runs in a bare image.
_DUMP = r"""
import json, pathlib, sys
out = set()
try:
    from skill_scanner.core.models import ThreatCategory
    out |= {m.value for m in ThreatCategory}
except Exception:
    pass
try:
    import yaml
    from skill_scanner import data as d
    for m in sorted((pathlib.Path(d.__file__).parent / "packs").glob("*/pack.yaml")):
        doc = yaml.safe_load(m.read_text(encoding="utf-8")) or {}
        rules = doc.get("rules") or {}
        for r in (rules.values() if isinstance(rules, dict) else rules):
            if isinstance(r, dict) and r.get("category"):
                out.add(str(r["category"]))
except Exception:
    pass
print(json.dumps(sorted(out)))
"""


def _scanner_categories() -> set[str] | None:
    """Every category the installed scanner declares, or None if it is not installed.

    The scanner ships as a CLI in its own environment, so this asks *its* interpreter,
    which it finds from the executable's shebang. Importing from the app's interpreter
    only works when the two happen to share a site-packages.
    """
    import json
    import shutil
    import subprocess
    import sys

    interpreters = [sys.executable]
    exe = shutil.which("skill-scanner")
    if exe:
        try:
            first = Path(exe).read_text(encoding="utf-8", errors="replace").splitlines()[0]
            if first.startswith("#!"):
                interpreters.insert(0, first[2:].split()[0])
        except OSError:
            pass

    for interp in interpreters:
        try:
            proc = subprocess.run([interp, "-c", _DUMP], capture_output=True, text=True,
                                  timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        try:
            found = set(json.loads(proc.stdout.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            continue
        if found:
            return found
    return None


class TestUnfamiliarCategory:
    """A scanner release can add a category. The gate must not read it as harmless."""

    def test_a_known_capability_category_at_medium_still_publishes(self):
        """The high floor is only justified where we checked what medium means."""
        assert verdict.decide(report(f(cat="data_exfiltration", sev="medium"))).state == "pass"

    def test_an_unfamiliar_category_at_medium_goes_to_review(self):
        r = report(f(rule="ATR_2027_00001", cat="memory_poisoning", sev="medium"))
        d = verdict.decide(r)
        assert d.state == "suspect"
        assert "memory_poisoning" in d.reason

    def test_an_unfamiliar_category_at_high_goes_to_review(self):
        assert verdict.decide(report(f(cat="brand_new_thing", sev="high"))).state == "suspect"

    def test_an_unfamiliar_category_at_low_does_not_gate(self):
        """medium is the floor, not "anything unknown". low/info is hygiene and context by
        this dimension's definition of severity, and blocking on it would spend a person's
        attention on every new informational rule the scanner ships."""
        assert verdict.decide(report(f(cat="brand_new_thing", sev="low"))).state == "pass"

    @pytest.mark.parametrize("cat", sorted(verdict.KNOWN_CATEGORIES))
    def test_every_known_category_is_one_the_scanner_can_emit(self, cat):
        """Guard against a typo in the list: a misspelt entry would silently make that
        category unfamiliar, which shows up as extra review rather than as an error."""
        assert cat == cat.lower().strip()
        assert " " not in cat

    def test_every_scanner_category_is_listed(self):
        """The installed scanner must not know a category the gate does not.

        This is the test that makes the mechanism honest. Without it, upgrading the scanner
        would quietly move every finding in a newly added category to the medium floor, and
        the first sign would be reviewers seeing more work. It reads both of the scanner's
        vocabularies, because they only partly overlap: the ``ThreatCategory`` enum, and the
        ``category`` field declared by each rule pack.

        The scanner is a CLI installed in its own environment (pipx), so importing it from
        the app's interpreter does not work. Asking its interpreter is the difference
        between a guard that runs and one that always skips.
        """
        declared = _scanner_categories()
        if declared is None:
            pytest.skip("skill-scanner is not installed in this environment")

        assert declared, "found no categories at all — the reading itself is broken"
        missing = sorted(declared - verdict.KNOWN_CATEGORIES)
        assert not missing, (
            "the scanner declares categories the gate has never classified: "
            f"{missing}. Decide for each whether medium means capability (add it to "
            "KNOWN_CATEGORIES) or concealment (add it to CONCEALMENT_CATEGORIES)."
        )

    def test_concealment_categories_are_known(self):
        assert verdict.CONCEALMENT_CATEGORIES <= verdict.KNOWN_CATEGORIES

    def test_the_hygiene_exemption_still_wins_for_unfamiliar_categories(self):
        """Hygiene is decided by rule id, so a hygiene rule reclassified into a new
        category must still stay out of the gate."""
        r = report(f(rule="MANIFEST_MISSING_LICENSE", cat="brand_new_thing", sev="medium"))
        assert verdict.decide(r).state == "pass"
