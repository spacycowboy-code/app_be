"""The streaming scan must not be worse than the blocking one it replaces.

Progress reporting is a nicety; the report arriving and the timeout firing are not. These
run a real subprocess rather than a fake, because both failure modes are properties of pipes
and process scheduling — a fake ``Popen`` cannot exhibit either.
"""
import subprocess
import sys
import time

import pytest

from app.services.skill_audit import scan


@pytest.fixture()
def as_scanner(monkeypatch):
    """Make scan() run a given shell script instead of skill-scanner."""
    def _use(script: str):
        real_popen = subprocess.Popen
        real_run = subprocess.run

        def popen(cmd, **kw):
            return real_popen(["sh", "-c", script], **kw)

        def run(cmd, **kw):
            return real_run(["sh", "-c", script], **kw)

        monkeypatch.setattr(scan.subprocess, "Popen", popen)
        monkeypatch.setattr(scan.subprocess, "run", run)
        monkeypatch.setattr(scan, "scanner_version", lambda _b: "test")
        monkeypatch.setattr(scan, "policy_fingerprint", lambda _p: "test")
    return _use


def _scan(**kw):
    return scan.scan(bin_path="s", target_dir="/t", policy_path="p", timeout=kw.pop("timeout", 20),
                     **kw)


class TestStreamingDoesNotLoseTheReport:
    def test_a_report_larger_than_a_pipe_buffer_survives(self, as_scanner):
        """The report is on stdout and can be hundreds of KB.

        Reading stderr to EOF while stdout sits in a 64 KB pipe deadlocks: the scanner
        blocks writing, we block reading. This is the test that pins stdout away from a
        pipe — with the pipe version it hangs until the test times out.
        """
        # The padding is built inside the child. Putting 400 KB on the command line hits
        # E2BIG, which is a limit of exec, not of the code under test.
        script = (
            'echo "Using Meta-Analyzer" >&2; '
            "python3 -c 'import json;"
            'print(json.dumps({"findings":[],"analyzers_used":["a"],"pad":"x"*400000}))\''
        )
        as_scanner(script)
        seen = []
        report = _scan(on_progress=seen.append)
        assert report.ran is True, report.error
        assert seen == ["semantic"]
        assert report.analyzers_used == ["a"]

    def test_the_report_is_the_same_with_and_without_progress(self, as_scanner):
        script = ('echo "Running meta-analysis" >&2; '
                  """echo '{"findings":[],"analyzers_used":["static_analyzer"],"max_severity":null}'""")
        as_scanner(script)
        streamed = _scan(on_progress=lambda _s: None)
        plain = _scan()
        assert streamed.ran and plain.ran
        assert streamed.analyzers_used == plain.analyzers_used


class TestStreamingKeepsTheTimeout:
    def test_a_silent_hang_still_times_out(self, as_scanner):
        """The deadline cannot depend on the scanner speaking.

        A scan that stops producing output must still end as ``ran=False``, because that is
        what the gate reads as "not checked" and sends to review. Checking the clock inside
        the stderr loop never fires here: there is no next line.
        """
        as_scanner('echo "Using Meta-Analyzer" >&2; sleep 60')
        started = time.monotonic()
        report = _scan(timeout=3, on_progress=lambda _s: None)
        elapsed = time.monotonic() - started
        assert report.ran is False
        assert "timed out" in (report.error or "")
        assert elapsed < 30, f"the watchdog did not fire (took {elapsed:.1f}s)"

    def test_a_hang_with_no_output_at_all_times_out(self, as_scanner):
        as_scanner("sleep 60")
        report = _scan(timeout=3, on_progress=lambda _s: None)
        assert report.ran is False and "timed out" in (report.error or "")

    def test_a_scan_that_finishes_in_time_is_not_killed(self, as_scanner):
        as_scanner('echo "Meta-analysis complete" >&2; echo \'{"findings":[]}\'')
        seen = []
        report = _scan(timeout=20, on_progress=seen.append)
        assert report.ran is True
        assert seen == ["reporting"]


class TestProgressComesFromTheScanner:
    def test_unrelated_stderr_invents_nothing(self, as_scanner):
        as_scanner('echo "Scanning /tmp/x" >&2; echo "warning: y" >&2; echo \'{"findings":[]}\'')
        seen = []
        assert _scan(on_progress=seen.append).ran is True
        assert seen == []

    def test_the_stages_are_reported_in_the_order_the_scanner_reaches_them(self, as_scanner):
        as_scanner(
            'echo "Using Meta-Analyzer for false positive filtering" >&2; '
            'echo "Running meta-analysis to filter false positives..." >&2; '
            'echo "Meta-analysis complete: 3 removed" >&2; '
            "echo '{\"findings\":[]}'"
        )
        seen = []
        _scan(on_progress=seen.append)
        assert seen == ["semantic", "arbitration", "reporting"]

    @pytest.mark.skipif(sys.platform == "win32", reason="uses sh")
    def test_stderr_is_still_captured_for_the_error_path(self, as_scanner):
        """When there is no report, the reason must come from stderr — the streaming path
        collects the lines it consumed instead of throwing them away."""
        as_scanner('echo "Error loading skill: SKILL.md missing required field: name" >&2; exit 1')
        report = _scan(on_progress=lambda _s: None)
        assert report.ran is False
        assert "missing required field" in (report.error or "")
