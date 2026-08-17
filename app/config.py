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
    # Additional attributes tried when the typed name looks like an address
    # (contains "@"). People routinely type their e-mail instead of their
    # signum; both resolve to the same account, so accept both rather than
    # refusing the login. Comma-separated.
    ldap_email_login_attrs: str = "userPrincipalName,mail,proxyAddresses"
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
    # Lifetime of a single-path "scoped" token, in MINUTES. These are pasted
    # into terminals and agent prompts, i.e. into places that keep history, so
    # the short window is the containment rather than an inconvenience. Raising
    # it much past a few minutes defeats the point.
    scoped_token_ttl_minutes: int = 10
    # --- Local admin bypass (break-glass; SKIPS LDAP) -----------------------
    # A built-in admin account authenticated WITHOUT LDAP, for bootstrap /
    # break-glass access. Its EID is `audit_admin_eid` (ADMINMM), so it has
    # admin rights everywhere. SECURITY: this is a static-credential backdoor —
    # set a strong password via env (LOCAL_ADMIN_PASSWORD) and set
    # LOCAL_ADMIN_ENABLED=false in production if you don't need it.
    local_admin_enabled: bool = True
    local_admin_username: str = "adminmm"
    local_admin_password: str = "adminmm"
    local_admin_display: str = "Administrator"


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
    # Seconds to wait for the judge; on timeout the audit fails closed.
    audit_judge_timeout: int = 200
    # Agent definition the ACP adapter starts kiro with. It must declare no tools
    # and demand JSON-only output: an interactive agent otherwise answers a JSON
    # request with a markdown report, which the scanner reads as "analysis failed".
    audit_acp_agent: str = "json-endpoint"
    # Remote judge-service URL. Empty => run kiro-cli in-process (host only).
    # Set this in the (cred-less) backend container to call the host judge service.
    # Where the scanner reaches the model: our ACP adapter. Empty disables the
    # semantic and meta layers, which the gate then reports as not checked.
    audit_llm_base_url: str = "http://audit-judge:8790/v1"
    # Optional shared secret required by the judge service (X-Judge-Token).
    audit_judge_token: str = ""
    # Red-line rules / criteria injected into the judge prompt.
    # Deterministic secret detection (detect-secrets, in-process, 27 detectors).
    # Broad matches route to review; the regex layer catches live-format keys
    # as red-line.
    audit_detect_secrets_enabled: bool = True
    # Cisco AI Defense skill-scanner (static analyzers, no key): the judge's
    # blind spots — bundled scripts / .pyc bytecode / unicode-hidden instructions.
    audit_cisco_enabled: bool = True
    audit_cisco_bin: str = "skill-scanner"
    # Our policy file: a permissive base with the overrides the measurements
    # required. Ships with the repo so the audit is reproducible.
    audit_policy_path: str = "skill_audit/policy.yaml"
    # One scan runs the deterministic analyzers plus two model calls; measured
    # median 41s and max 85s over 46 real skills. The ceiling is generous on
    # purpose: a timeout is recorded as "not checked", which costs a review.
    audit_scan_timeout: int = 900
    # Behavioral analysis is off by default: measured 29s on a 26-file bundle but
    # 228s on a 10-file one (a single large file makes the taint analysis diverge),
    # for one extra finding. Enable it with a budget, not unconditionally.
    audit_behavioral_enabled: bool = False
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

    @property
    def ldap_email_login_attr_list(self) -> list[str]:
        return [a.strip() for a in self.ldap_email_login_attrs.split(",") if a.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
