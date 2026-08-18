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
