"""The adapter's job is protocol translation, and these are the ways it can lie."""
import json

import pytest

from app.services.skill_audit import acp


class TestExtractJson:
    """A chat model packages JSON like chat. The scanner needs it unwrapped."""

    def test_a_fenced_block_is_unwrapped(self):
        assert acp.extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_trailing_prose_after_the_block_is_dropped(self):
        raw = '```json\n{"a": 1}\n```\nThe skill will continue to behave normally.'
        assert acp.extract_json(raw) == '{"a": 1}'

    def test_a_preamble_before_the_object_is_dropped(self):
        assert acp.extract_json('Sure! {"a": 1} done') == '{"a": 1}'

    def test_braces_inside_strings_do_not_end_the_object(self):
        raw = '{"note": "a } inside a string", "n": 1}'
        assert acp.extract_json(raw) == raw

    def test_an_escaped_quote_does_not_end_the_string(self):
        raw = '{"note": "she said \\"}\\" once", "n": 1}'
        assert json.loads(acp.extract_json(raw))["n"] == 1

    def test_a_reply_with_no_json_is_returned_unchanged(self):
        """So the caller can see it was prose and fail closed rather than guess."""
        assert acp.extract_json("## Security Analysis\n\nThis skill is fine.").startswith("##")


class TestRenderPrompt:
    def test_a_single_message_is_passed_through_verbatim(self):
        """The scanner's prompt already carries its own delimiters; adding labels would
        put our text inside the untrusted-input region."""
        assert acp.render_prompt([{"role": "user", "content": "RULES"}]) == "RULES"

    def test_several_messages_keep_their_roles(self):
        out = acp.render_prompt([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
        assert "[system]\nS" in out and "[user]\nU" in out

    def test_content_parts_are_joined(self):
        out = acp.render_prompt([{"role": "user", "content": [{"text": "a"}, {"text": "b"}]}])
        assert out == "ab"


class TestModelValidation:
    def test_an_unknown_model_is_rejected(self, monkeypatch):
        """`kiro-cli acp` accepts a bad name silently; without this the audit record's
        model attribution would be false."""
        class Done:
            stdout = ""
            stderr = "error: Model 'nope' does not exist. Available models: auto, claude-sonnet-5"

        monkeypatch.setattr(acp.subprocess, "run", lambda *a, **k: Done())
        with pytest.raises(acp.AcpError):
            acp.validate_model("kiro-cli", "nope")

    def test_a_known_model_passes(self, monkeypatch):
        class Done:
            stdout = "ok"
            stderr = ""

        monkeypatch.setattr(acp.subprocess, "run", lambda *a, **k: Done())
        acp.validate_model("kiro-cli", "claude-sonnet-5")

    def test_no_model_pinned_is_not_checked(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("should not probe when no model is pinned")

        monkeypatch.setattr(acp.subprocess, "run", boom)
        acp.validate_model("kiro-cli", "")


class TestCompleteJson:
    def _sessions(self, monkeypatch, replies):
        seen = []

        class FakeSession:
            def __init__(self, **kw):
                self.kw = kw

            def complete(self, prompt):
                seen.append(prompt)
                return replies[len(seen) - 1]

            def close(self):
                pass

        monkeypatch.setattr(acp, "AcpSession", FakeSession)
        return seen

    def test_a_json_reply_is_taken_on_the_first_try(self, monkeypatch):
        seen = self._sessions(monkeypatch, ['{"ok": 1}'])
        out = acp.complete_json(bin_path="k", model="m", agent="a", cwd="/tmp",
                                timeout=1, prompt="P")
        assert out == '{"ok": 1}' and len(seen) == 1

    def test_a_prose_reply_is_retried_once_with_the_requirement_restated(self, monkeypatch):
        seen = self._sessions(monkeypatch, ["## A report", '{"ok": 1}'])
        out = acp.complete_json(bin_path="k", model="m", agent="a", cwd="/tmp",
                                timeout=1, prompt="P")
        assert out == '{"ok": 1}'
        assert len(seen) == 2 and "not valid JSON" in seen[1]

    def test_two_prose_replies_are_reported_not_papered_over(self, monkeypatch):
        """The scanner must be able to see this failed so the gate can route to review."""
        self._sessions(monkeypatch, ["## A report", "## Still a report"])
        out = acp.complete_json(bin_path="k", model="m", agent="a", cwd="/tmp",
                                timeout=1, prompt="P")
        assert not out.startswith("{")


class TestLlmEnv:
    """How the scanner is told to reach the model."""

    def test_the_model_gets_the_provider_prefix(self):
        from app.services.skill_audit.scan import llm_env
        env = llm_env(base_url="http://x/v1/", api_key="k", model="claude-sonnet-5")
        assert env["SKILL_SCANNER_LLM_MODEL"] == "openai/claude-sonnet-5"
        assert env["SKILL_SCANNER_LLM_BASE_URL"] == "http://x/v1"
        assert env["SKILL_SCANNER_LLM_PROVIDER"] == "openai"

    def test_an_already_prefixed_model_is_not_doubled(self):
        from app.services.skill_audit.scan import llm_env
        env = llm_env(base_url="http://x/v1", api_key="k", model="openai/claude-sonnet-5")
        assert env["SKILL_SCANNER_LLM_MODEL"] == "openai/claude-sonnet-5"

    def test_an_empty_key_still_sends_something(self):
        """The endpoint requires a bearer value; an empty one would fail the check."""
        from app.services.skill_audit.scan import llm_env
        assert llm_env(base_url="http://x/v1", api_key="", model="m")["SKILL_SCANNER_LLM_API_KEY"]


class TestAdapterAuth:
    """The endpoint is model access on the network."""

    def _client(self, monkeypatch, token):
        from fastapi.testclient import TestClient
        from app import acp_openai_service as svc

        class S:
            audit_judge_token = token
            audit_kiro_bin = "kiro-cli"
            audit_kiro_model = "claude-sonnet-5"
            audit_acp_agent = "json-endpoint"
            audit_work_dir = "/tmp"
            audit_judge_timeout = 5

        monkeypatch.setattr(svc, "get_settings", lambda: S())
        monkeypatch.setattr(svc, "complete_json", lambda **kw: '{"findings": []}')
        client = TestClient(svc.app)
        return client

    def test_a_request_without_the_key_is_rejected(self, monkeypatch):
        client = self._client(monkeypatch, "secret")
        r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 401

    def test_the_configured_key_is_accepted(self, monkeypatch):
        client = self._client(monkeypatch, "secret")
        r = client.post("/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "x"}]},
                        headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == '{"findings": []}'

    def test_no_token_configured_leaves_it_open(self, monkeypatch):
        """Staging runs without one; production sets it. Stated so it is a choice."""
        client = self._client(monkeypatch, "")
        r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200
