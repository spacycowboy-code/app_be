"""Application configuration, loaded from environment / .env file."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "RP AI Portal Backend"
    debug: bool = True

    # Short git commit SHA of the deployed build, injected at Docker build time
    # (ARG GIT_COMMIT -> ENV APP_GIT_COMMIT). "dev" when running locally.
    app_git_commit: str = "dev"

    # SQLAlchemy database URL, e.g. mysql+pymysql://user:pass@host:port/db
    database_url: str

    # Bearer token guarding POST /api/skills/import-official (official-skill
    # ingest, called by the rp-ai-skill sync CI). Empty = endpoint disabled.
    skill_import_token: str = ""

    # Comma-separated list of allowed CORS origins.
    cors_origins: str = "http://localhost:4200"

    # --- Skill storage repo -------------------------------------------------
    # Git-managed directory where uploaded skills are stored.
    skill_repo_path: str = "/proj/radio_ivc_rvc_workspace/ezhgmio/rp-ai-skill"
    # Push to the git remote after each upload commit.
    skill_repo_push: bool = True
    # Git remote/branch to push to.
    skill_repo_remote: str = "origin"
    skill_repo_branch: str = "main"
    # Identity used for skill-repo commits (does not touch global git config).
    git_author_name: str = "RP AI Portal"
    git_author_email: str = "rp-ai-portal@ericsson.com"
    # Max upload artifact size in bytes (default 50 MB).
    max_upload_bytes: int = 50 * 1024 * 1024

    # --- GitLab (Agent市场 sync) --------------------------------------------
    # Base URL of the GitLab instance.
    gitlab_url: str = "https://gitlab.internal.ericsson.com"
    # Personal access token with `read_api` scope. Empty = sync disabled.
    gitlab_token: str = ""
    # Subgroup full paths whose projects become Agents / Tools respectively.
    gitlab_agents_group: str = "rp-ai/rp-ai-agents"
    gitlab_tools_group: str = "rp-ai/rp-ai-tools"
    # Verify TLS when calling the GitLab API (internal certs may be self-signed).
    gitlab_verify_tls: bool = False

    # --- Authentication: LDAP (Active Directory) + JWT session --------------
    # Master switch. When off, /api/auth/login rejects and the request gate is
    # disabled (dev only — never ship with this off).
    auth_enabled: bool = True
    # LDAP server URL. Use the domain (not a single DC IP); ldaps => TLS.
    ldap_url: str = "ldaps://ericsson.se:636"
    # Search base for user lookups.
    ldap_base_dn: str = "DC=ericsson,DC=se"
    # Service/bind account the app uses to search for users (UPN form works on
    # AD). The password is a SECRET — set via env/.env, never commit it.
    ldap_bind_user: str = "ecnrpai@ericsson.se"
    ldap_bind_password: str = ""
    # Filter to resolve the login name to a user entry ({username} is escaped).
    ldap_user_filter: str = "(sAMAccountName={username})"
    # Attribute whose value becomes the portal EID (AD `uid` is already upper).
    ldap_eid_attr: str = "uid"
    # Attributes copied onto the user row for display.
    ldap_display_attr: str = "displayName"
    ldap_department_attr: str = "department"
    # Validate the LDAPS server certificate. Kept False for parity with
    # gitlab_verify_tls until the internal CA bundle is wired in; turn on in prod.
    ldap_verify_tls: bool = False
    # Seconds to wait for LDAP operations.
    ldap_timeout: int = 10

    # HMAC secret for signing session JWTs. REQUIRED in any real deployment
    # (empty => login disabled). Set via env/.env, never commit it.
    auth_jwt_secret: str = ""
    auth_jwt_algorithm: str = "HS256"
    # Session lifetime in hours.
    auth_token_ttl_hours: int = 8


    # --- Skill security audit (upload-time gate) ----------------------------
    # Master switch. When off, uploads skip the audit (state left null).
    audit_enabled: bool = True
    # kiro-cli binary for the semantic judge (must be logged in on the host).
    audit_kiro_bin: str = "kiro-cli"
    # Judge model; empty => kiro-cli default (auto). Pin before hard-gating.
    # Pinned, never "auto": a gate has to give the same verdict for the same
    # input, and "auto" may switch models without notice.
    audit_kiro_model: str = "claude-sonnet-5"
    # Second opinion, asked only when the primary judge wants to reject. A
    # disagreement routes to human review instead of blocking the author.
    # Note: claude-opus-5 is an experimental preview; switch when it goes GA.
    audit_kiro_model_confirm: str = "claude-opus-5"
    # Seconds to wait for the judge; on timeout the audit fails closed.
    audit_judge_timeout: int = 200
    # Remote judge-service URL. Empty => run kiro-cli in-process (host only).
    # Set this in the (cred-less) backend container to call the host judge service.
    audit_judge_url: str = ""
    # Optional shared secret required by the judge service (X-Judge-Token).
    audit_judge_token: str = ""
    # Red-line rules / criteria injected into the judge prompt.
    audit_rules_path: str = "skill_audit/rules.md"
    # Deterministic secret detection (detect-secrets, in-process, 27 detectors).
    # Broad matches route to review; the regex layer catches live-format keys
    # as red-line.
    audit_detect_secrets_enabled: bool = True
    # Cisco AI Defense skill-scanner (static analyzers, no key): the judge's
    # blind spots — bundled scripts / .pyc bytecode / unicode-hidden instructions.
    audit_cisco_enabled: bool = True
    audit_cisco_bin: str = "skill-scanner"
    # Working dir for the judge subprocess (kiro-cli writes a small state db).
    audit_work_dir: str = "/tmp/skill-audit"
    # EID allowed to see 可疑(suspect) skills while browsing (internal-only).
    audit_admin_eid: str = "ADMINMM"
    # 可疑 -> Draft MR review. Needs a GitLab token with `api` scope.
    audit_review_enabled: bool = True
    # Skill repo project (path or numeric id) where suspect branches + MRs land.
    audit_mr_project: str = "rp-ai/rp-ai-skill"
    # Reviewer GitLab username assigned on the Draft MR.
    audit_mr_reviewer: str = "ezhgmio"
    # Shared secret for POST /api/audit/reconcile (the GitLab webhook).
    # Empty disables the endpoint; the periodic sweep still runs.
    audit_webhook_token: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
