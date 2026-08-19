"""What we assume about the scanner, asserted against the scanner that is installed.

Both things here fail *silently* if the scanner changes underneath us, which is why they are
tests rather than comments:

* If the phase wording on stderr changes, ``STAGE_MARKERS`` stops matching. Nothing errors —
  the audit still works, the progress display just goes quiet forever, and the person waiting
  is back to a blank screen.
* If ``skill-scanner`` is not on PATH at all, an audit produces no report. That is handled
  (``ran=False`` means "not checked"), but it should not be discovered by a reviewer noticing
  that everything went to review.

Both skip when the scanner is absent, because the unit suite also runs in a bare image.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.skill_audit.scan import STAGE_MARKERS

_SCANNER = shutil.which("skill-scanner")
needs_scanner = pytest.mark.skipif(_SCANNER is None, reason="skill-scanner is not installed here")


def _package_dir() -> Path | None:
    """Where the installed scanner's code lives.

    It ships as a CLI in its own environment (pipx), so the app's interpreter cannot import
    it; the executable's shebang points at the interpreter that can.
    """
    if _SCANNER is None:
        return None
    try:
        first = Path(_SCANNER).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    interp = first[2:].split()[0]
    probe = "import pathlib, skill_scanner; print(pathlib.Path(skill_scanner.__file__).parent)"
    try:
        proc = subprocess.run([interp, "-c", probe], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    path = Path(proc.stdout.strip().splitlines()[-1])
    return path if path.is_dir() else None


@needs_scanner
def test_the_scanner_runs_and_reports_its_version():
    """`ran=False` is handled, but a missing scanner should not be found by accident."""
    proc = subprocess.run([_SCANNER, "--version"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-300:]
    assert "skill-scanner" in (proc.stdout + proc.stderr).lower()


@needs_scanner
@pytest.mark.parametrize("marker", [m for m, _stage in STAGE_MARKERS])
def test_each_stage_marker_still_appears_in_the_scanner(marker):
    """The progress display reads these strings off stderr.

    Asserted against the scanner's own source rather than by running an audit, because the
    phases that emit them need the model, and this has to hold in CI where there is none.
    A rewording upstream makes progress silently stop, so it has to break a test instead.
    """
    pkg = _package_dir()
    if pkg is None:
        pytest.skip("could not locate the installed scanner package")
    hits = [p for p in pkg.rglob("*.py")
            if marker in p.read_text(encoding="utf-8", errors="replace")]
    assert hits, (
        f"the scanner no longer prints {marker!r}: progress reporting would go quiet without "
        "any error. Re-read its phase logging and update STAGE_MARKERS."
    )


# ``verdict._no_report_cause`` still has to tell "the scanner refused to load this bundle" apart
# from "the scan broke", and it does that from the scanner's own wording. Two strings, asserted
# here so a rewording upstream fails a test instead of quietly turning an author-fixable case
# into "no guidance yet".
_LOAD_ERROR_MARKERS = ["Error loading skill", "missing required field"]


@needs_scanner
@pytest.mark.parametrize("marker", _LOAD_ERROR_MARKERS)
def test_each_load_error_marker_still_appears_in_the_scanner(marker):
    pkg = _package_dir()
    if pkg is None:
        pytest.skip("could not locate the installed scanner package")
    hits = [p for p in pkg.rglob("*.py")
            if marker in p.read_text(encoding="utf-8", errors="replace")]
    assert hits, (
        f"the scanner no longer says {marker!r}. A bundle it cannot load would be classified as "
        "a platform failure instead of something the author can fix. Re-read its skill loader "
        "and update verdict._no_report_cause."
    )


@needs_scanner
def test_the_load_error_wording_actually_classifies_the_real_case():
    """End to end on the real thing: run the scanner on the frontmatter that failed in
    production, and check the message it produces lands on the author-fixable cause."""
    import json
    import subprocess
    import tempfile

    from app.services.skill_audit import verdict

    bad = ("---\nname:A Replir Allocation Tool - HTML Generator Skill\n\n"
           "description:By using this skill you can made a html tool\n---\n\n# T\n\nbody\n")
    with tempfile.TemporaryDirectory() as d:
        Path(d, "SKILL.md").write_text(bad, encoding="utf-8")
        proc = subprocess.run([_SCANNER, "scan", d, "--format", "json"],
                              capture_output=True, text=True, timeout=300)
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "missing required field" in combined, combined[-400:]
    cause = verdict._no_report_cause(
        next(ln for ln in combined.splitlines() if "Error loading skill" in ln))
    assert cause == verdict.CAUSE_BUNDLE_UNLOADABLE
    assert verdict.owner_of(cause) == verdict.OWNER_AUTHOR, (
        "a bundle the scanner cannot load is the author's to fix, not an outage of ours"
    )


# What the upload check refuses has to be what the scanner refuses — no wider. A check that is
# stricter than the tool blocks skills the tool would have accepted, and the author has no way to
# tell that the platform, not the scanner, turned them away. Each case below was measured against
# the installed binary; the first attempt at this check got two of them wrong (it refused a bare
# colon inside a value, and an empty value, both of which the scanner loads).
_FRONTMATTER_CASES = [
    ("no space after the colon",
     "---\nname:probe\ndescription:no space\n---\n\nbody\n", True),
    ("no space on name only",
     "---\nname:probe\ndescription: ok\n---\n\nbody\n", True),
    ("a bare colon inside a value",
     "---\nname: probe\ndescription: A tool: does things\n---\n\nbody\n", False),
    ("an empty value",
     "---\nname:\ndescription: x\n---\n\nbody\n", False),
    ("unparseable YAML",
     "---\nname: [unclosed\ndescription: x\n---\n\nbody\n", True),
    ("a tab-indented line",
     "---\nname: probe\n\tdescription: x\n---\n\nbody\n", True),
    ("a missing required key",
     "---\nname: probe\n---\n\nbody\n", True),
    ("bare labels with no --- block",
     "name: probe\ndescription: x\n\nbody\n", True),
    ("a well-formed block",
     "---\nname: probe\ndescription: A benign probe\n---\n\nbody\n", False),
    ("a quoted value containing a colon",
     "---\nname: probe\ndescription: \"A tool: does things\"\n---\n\nbody\n", False),
]


@needs_scanner
@pytest.mark.parametrize("label,body,scanner_refuses", _FRONTMATTER_CASES)
def test_the_upload_check_agrees_with_the_scanner(label, body, scanner_refuses):
    """Both sides of the boundary, against the installed scanner."""
    import subprocess
    import tempfile

    from app.services.skill_manifest import frontmatter_problem

    with tempfile.TemporaryDirectory() as d:
        Path(d, "SKILL.md").write_text(body, encoding="utf-8")
        proc = subprocess.run([_SCANNER, "scan", d, "--format", "json"],
                              capture_output=True, text=True, timeout=300)
    refused = "Error loading skill" in (proc.stderr or "")
    assert refused is scanner_refuses, (
        f"the scanner's tolerance for {label!r} changed: it now "
        f"{'refuses' if refused else 'loads'} it. Re-measure and update both this table and "
        "skill_manifest.frontmatter_problem."
    )
    blocked = frontmatter_problem(body) is not None
    assert blocked is refused, (
        f"for {label!r} the upload check says {'block' if blocked else 'allow'} while the scanner "
        f"says {'refuse' if refused else 'load'}. Stricter than the tool blocks skills it would "
        "have accepted; looser hands a reviewer a skill nobody could examine."
    )
