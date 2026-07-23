"""Unit tests: secret redaction + audit-state derivation."""
from app.services.skill_audit.scanners import redact_secrets
from app.services.skill_audit.pipeline import _derive_state
from app.services.skill_audit.types import Finding


def test_redact_real_key_masked_and_flagged():
    red, finds = redact_secrets("token: tvly-abcd1234efgh5678ijkl done")
    assert "tvly-abcd1234efgh5678ijkl" not in red
    assert "<REDACTED_SECRET>" in red
    assert any(f.category == "secret" and f.severity == "critical" for f in finds)


def test_redact_placeholder_not_flagged():
    red, finds = redact_secrets("replace tvly-YOUR_API_KEY_HERE with your key")
    assert finds == []
    assert "tvly-YOUR_API_KEY_HERE" in red


def test_state_danger_on_redline_secret():
    assert _derive_state([Finding("pipeline", "secret", "critical")], "pass") == "danger"


def test_state_danger_on_judge_block():
    assert _derive_state([], "block") == "danger"


def test_state_suspect_on_judge_flag():
    assert _derive_state([], "flag") == "suspect"


def test_state_suspect_failclosed_on_judge_error():
    assert _derive_state([], "error") == "suspect"


def test_state_suspect_on_nonredline_finding():
    assert _derive_state([Finding("cisco", "malicious_artifact", "medium")], "pass") == "suspect"


def test_state_pass_when_clean():
    assert _derive_state([], "pass") == "pass"


def test_artifact_scan_flags_script(tmp_path):
    from app.services.skill_audit.scanners import artifact_scan
    (tmp_path / "SKILL.md").write_text("x")
    (tmp_path / "setup.sh").write_text("#!/bin/bash\necho hi")
    finds = artifact_scan(str(tmp_path))
    assert any(f.category == "malicious_artifact" for f in finds)


def test_artifact_scan_ignores_docs(tmp_path):
    from app.services.skill_audit.scanners import artifact_scan
    (tmp_path / "SKILL.md").write_text("x")
    (tmp_path / "notes.txt").write_text("hello")
    assert artifact_scan(str(tmp_path)) == []


# --- Cisco finding mapping (threat whitelist; hygiene noise dropped) --------

def test_cisco_map_threats():
    from app.services.skill_audit.scanners import _map_cisco
    assert _map_cisco("YARA_prompt_injection_generic", "prompt_injection") == "prompt_injection"
    assert _map_cisco("YARA_prompt_injection_unicode_steganography", "obfuscation") == "hidden_instruction"
    assert _map_cisco("COMPOUND_FETCH_EXECUTE", "supply_chain") == "dangerous_instruction"
    assert _map_cisco("COMMAND_INJECTION_EVAL", "code_execution") == "dangerous_instruction"
    assert _map_cisco("YARA_x", "privilege_escalation") == "dangerous_instruction"
    assert _map_cisco("BYTECODE_NO_SOURCE", "supply_chain") == "dangerous_instruction"  # cat wins
    assert _map_cisco("BYTECODE_NO_SOURCE", "bytecode") == "malicious_artifact"


def test_cisco_map_drops_hygiene():
    from app.services.skill_audit.scanners import _map_cisco
    assert _map_cisco("MANIFEST_MISSING_LICENSE", "policy_violation") is None
    assert _map_cisco("MANIFEST_INVALID_NAME", "policy_violation") is None
    assert _map_cisco("SOCIAL_ENG_VAGUE_DESCRIPTION", "social_engineering") is None
    assert _map_cisco("FILE_MAGIC_MISMATCH", "obfuscation") is None
    assert _map_cisco("LOW_ANALYZABILITY", "policy_violation") is None


def test_audit_caches_by_content(monkeypatch):
    """evaluate + publish on identical content must run the judge only once."""
    from app.services.skill_audit import pipeline
    from app.services.skill_audit.types import JudgeResult

    pipeline._verdict_cache.clear()
    calls = {"n": 0}

    def _fake_judge(**kw):
        calls["n"] += 1
        return JudgeResult(verdict="pass")

    monkeypatch.setattr(pipeline, "invoke_judge", _fake_judge)
    md = "---\nname: T\ndescription: d\n---\nbody\n"

    first = pipeline.audit_skill(name="T", skill_md=md)
    second = pipeline.audit_skill(name="T", skill_md=md)   # cache hit
    assert first.state == "pass" and second.state == "pass"
    assert calls["n"] == 1                                  # judge ran once

    pipeline.audit_skill(name="T", skill_md=md + " changed")  # different content
    assert calls["n"] == 2                                  # re-audited
