"""Frontmatter that no downstream reader can parse is caught at upload, not by the gate.

Our own parser is forgiving on purpose — it falls back to a regex so a slightly malformed
block still yields a marketplace card. The scanner is not: it parses the block as YAML and
refuses to load the skill when the result is not a mapping. The audit then ends as "no
report", which the gate correctly reads as "not checked" and sends to a human — who is handed
a skill nobody has looked at, with nothing to review.

The real case, 2026-08-19: ``name:A Replir Allocation Tool`` — no space after the colon. YAML
reads the whole block as one string, so ``name`` is absent while looking present. The author
uploaded it twice, unchanged, because what came back never said what was wrong.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.routers import upload as upload_mod
from app.services.auth_tokens import create_token
from app.services.skill_manifest import frontmatter_problem, parse_frontmatter
from app.services.skill_audit.types import AuditResult

EID = "EYINCNG"

REAL_CASE = (
    "---\n"
    "name:A Replir Allocation Tool - HTML Generator Skill\n"
    "\n"
    "description:By using this skill you can made a html tool\n"
    "---\n\n## 概述\n\n本 Skill 用于生成一个工具。\n"
)
GOOD = ("---\nname: A Replir Allocation Tool\ndescription: Generates an HTML tool\n---\n\n"
        "# Tool\n\nbody\n")


class TestTheCheckItself:
    def test_the_real_production_case_is_caught(self):
        problem = frontmatter_problem(REAL_CASE)
        assert problem is not None
        assert "少了空格" in problem, problem
        assert "name" in problem

    def test_a_well_formed_block_passes(self):
        assert frontmatter_problem(GOOD) is None

    def test_no_block_at_all_is_not_a_problem_here(self):
        """with_frontmatter adds a well-formed block, so this is not the place to refuse."""
        assert frontmatter_problem("# Just a heading\n\nbody\n") is None

    def test_empty_text_is_not_a_problem_here(self):
        assert frontmatter_problem("") is None

    def test_broken_yaml_is_reported_with_the_parser_s_first_line(self):
        problem = frontmatter_problem("---\nname: [unclosed\ndescription: x\n---\n\nbody\n")
        assert problem is not None
        assert "不是有效的 YAML" in problem

    def test_an_empty_value_is_allowed_because_the_scanner_loads_it(self):
        """Measured: the scanner loads `name:` with no value. Refusing here would block a skill
        it would have accepted, and the author could not tell that we, not it, turned them away."""
        assert frontmatter_problem("---\nname:\ndescription: x\n---\n\nbody\n") is None

    def test_a_missing_key_is_reported(self):
        problem = frontmatter_problem("---\nname: probe\n---\n\nbody\n")
        assert problem is not None
        assert "缺少" in problem and "description" in problem

    def test_a_bare_colon_inside_a_value_is_allowed(self):
        """`description: A tool: does things` is not valid yaml.safe_load input, but the scanner's
        own parser accepts it. The check follows the scanner, not the stricter library."""
        assert frontmatter_problem(
            "---\nname: probe\ndescription: A tool: does things\n---\n\nbody\n") is None

    @pytest.mark.parametrize("block", [
        "---\nname: x\ndescription: y\n---\n",                      # minimal
        "---\r\nname: x\r\ndescription: y\r\n---\r\n",              # CRLF
        "---\nname: x\ndescription: >-\n  folded text here\n---\n",  # block scalar
        "---\nname: 'x'\ndescription: \"y\"\n---\n",                 # quoted
    ])
    def test_valid_shapes_are_not_refused(self, block):
        assert frontmatter_problem(block + "\nbody\n") is None

    def test_our_lenient_parser_still_recovers_the_name(self):
        """The leniency is why this reached the repo in the first place — keep it documented."""
        name, desc = parse_frontmatter(REAL_CASE)
        assert name == "A Replir Allocation Tool - HTML Generator Skill"
        assert desc


@pytest.fixture()
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "fm-test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_jwt_algorithm", "HS256", raising=False)
    # If the audit is reached at all, this test has already failed its point.
    monkeypatch.setattr(upload_mod, "audit_skill",
                        lambda **kw: AuditResult(state="pass"))
    return TestClient(app)


def _auth() -> dict:
    token, _ = create_token(EID, display_name="Terry Yin")
    return {"Authorization": f"Bearer {token}"}


def _post(client, path, body, name="fm-probe", extra=None):
    data = {"eid": EID, "name": name}
    data.update(extra or {})
    return client.post(path, data=data,
                       files={"file": ("SKILL.md", body, "text/markdown")}, headers=_auth())


class TestTheAuthorIsToldAtUploadTime:
    def test_evaluate_refuses_with_the_precise_reason(self, client):
        resp = _post(client, "/api/skills/evaluate", REAL_CASE)
        assert resp.status_code == 400, resp.text
        assert "少了空格" in resp.json()["detail"]

    def test_publish_refuses_the_same_way(self, client):
        resp = _post(client, "/api/skills/upload", REAL_CASE, extra={"categoryId": "public"})
        assert resp.status_code == 400, resp.text
        assert "少了空格" in resp.json()["detail"]

    def test_a_well_formed_skill_is_unaffected(self, client, monkeypatch):
        resp = _post(client, "/api/skills/evaluate", GOOD)
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "pass"

    def test_the_older_message_still_covers_a_missing_label(self, client):
        """Unchanged behaviour: no name:/description: at all is still refused up front."""
        resp = _post(client, "/api/skills/evaluate", "# no frontmatter labels\n\nbody\n")
        assert resp.status_code == 400
        assert "name:" in resp.json()["detail"]


class TestTheBlocklessDoor:
    """Found by review, confirmed against the installed scanner.

    ``has_name_and_description`` matches the two labels anywhere in the file, so
    ``with_frontmatter`` treats a SKILL.md with bare `name:` / `description:` lines as already
    having frontmatter and leaves it alone. The scanner then refuses to load it — the same
    incident through a different door.
    """

    BLOCKLESS = "name: probe\ndescription: A benign probe\n\n# Probe\n\nbody\n"

    def test_bare_labels_without_a_block_are_refused(self):
        problem = frontmatter_problem(self.BLOCKLESS)
        assert problem is not None
        assert "`---`" in problem

    def test_the_message_shows_the_correct_shape(self):
        problem = frontmatter_problem(self.BLOCKLESS) or ""
        assert "name: 你的名字" in problem

    def test_a_file_with_no_frontmatter_at_all_is_still_allowed(self):
        """with_frontmatter genuinely does add a block for these, so refusing would be wrong."""
        assert frontmatter_problem("# Just a heading\n\nbody\n") is None

    def test_upload_refuses_it(self, client):
        resp = _post(client, "/api/skills/evaluate", self.BLOCKLESS, name="blockless")
        assert resp.status_code == 400, resp.text
        assert "`---`" in resp.json()["detail"]
