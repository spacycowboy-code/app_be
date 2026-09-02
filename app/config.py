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
    # Push to the git remote after each upload commit.
    # Git remote/branch to push to.
    # --- 审计引擎(独立服务,仓库 rp-ai-miao/skill-audit)------------------------
    # 同一个 overlay 内按服务名访问;不发布端口,鉴权全靠 token。
    # 扫描器与判定器的那一批配置(模型、超时、策略文件、工作目录…)**不在这里** —— 它们跟着引擎
    # 走了(仓库 rp-ai-miao/skill-audit,环境变量在 SKILL_AUDIT_*_ENV 里)。这里曾经留着 14 个那样的
    # 字段,没有任何代码读它们:在门户环境里改 AUDIT_KIRO_MODEL 不会有任何效果,而看起来像是能改。
    audit_engine_url: str = "http://skill-audit:8000"
    # 与引擎的 ENGINE_TOKEN 相同。为空时引擎会拒绝请求(它 token 为空则拒绝启动)。
    audit_engine_token: str = ""
    # **单次读**超时,不是总时长。引擎每 15 秒发一个 ping,所以一分钟没有任何字节就说明它挂死了;
    # 而一次审计本身可以跑到十几分钟(扫描 900 秒 + 判定 200 秒),那不受这个值限制。
    audit_engine_read_timeout: int = 60
    skill_repo_branch: str = "main"
    # Identity used for skill-repo commits (does not touch global git config).
    git_author_name: str = "RP AI Portal"
    git_author_email: str = "rp-ai-portal@ericsson.com"
    # Max upload artifact size in bytes (default 50 MB).
    max_upload_bytes: int = 50 * 1024 * 1024

    # --- GitLab (Agent市场 sync) --------------------------------------------
    # Base URL of the GitLab instance.
    #: 按 ref 缓存技能内容的目录。**必须是容器本地目录**,不能是 bind mount ——
    #: 挂上去就又把后端钉回一台机器,而这次重构的目的正是解开它。
    #: 内容按不可变引用寻址,所以缓存不需要失效逻辑,删了下次重取。
    content_cache_dir: str = "/tmp/skill-content-cache"
    content_cache_max_mb: int = 512

    gitlab_url: str = "https://gitlab.internal.ericsson.com"
    # Personal access token with `read_api` scope. Empty = sync disabled.
    gitlab_token: str = ""
    # Subgroup full paths whose projects become Agents / Tools respectively.
    gitlab_agents_group: str = "rp-ai/rp-ai-agents"
    gitlab_tools_group: str = "rp-ai/rp-ai-tools"

    # --- GitLab (用户上传的包) ------------------------------------------------
    # 上传的包每个在这两个组之一下有自己的项目:agent 进 user-agents,tool 进 user-tools。
    # **独立于上面那个 token**:上面那个是个人 PAT、全域读写、要读 sync 的两个组;这里两个
    # 是各自那个组的 group access token(Owner + api,write_repository),作用域只有那一个组。
    # 合成一个的代价是给"上传包"配上全域写权限,而分开的代价只是多几个变量。
    #
    # **两个类型各一个 token,而不是在父组 rp-ai 上开一个覆盖两边的。** 那样只要一个变量,
    # 代价是写权限扩到整个 rp-ai(含技能仓和 sync 的那两个组)—— 正好是拆开这个凭据要避免
    # 的事。谁能写哪里,应该和"哪个功能需要写哪里"一样窄。
    gitlab_user_agents_group: str = "rp-ai/rp-ai-user-agents"
    # 空 = 关闭该类型的包上传(端点答 503 并说明缺哪个变量,而不是把包丢掉)。
    gitlab_user_agents_token: str = ""
    # 该 token 的到期日,只用于提前提醒。GitLab 强制 group token 有到期日,所以这是
    # 一个**已知的将来故障**:不预告的话,那天上传会突然 401,而现象离原因很远。
    gitlab_user_agents_token_expires: str = "2027-08-20"
    gitlab_user_tools_group: str = "rp-ai/rp-ai-user-tools"
    gitlab_user_tools_token: str = ""
    gitlab_user_tools_token_expires: str = ""
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

    # --- Sign-in statistics access (Dashboard) ------------------------------
    # EIDs allowed to read /api/login-stats and /api/department-report — the
    # active-user counts, the signums behind them, and the per-person report.
    # signums behind them. Enforced in the endpoint: the sidebar also hides the
    # Dashboard from everyone else, but hiding a link is not access control.
    # Comma-separated; compared uppercase.
    # EZHGMIO Miao Zhang M, ADMINMM the break-glass admin, EHUIWUU Vivie Wu A,
    # EBAOXUE Baosheng Xue. Also gates the department usage report, which names
    # individuals.
    dashboard_eids: str = "EZHGMIO,ADMINMM,EHUIWUU,EBAOXUE"
    # --- Department usage report ---------------------------------------------
    # Snapshot of the Microsoft Graph reporting tree — the same graph the Teams org
    # chart draws. Every head count and every percentage in the report is derived
    # from it, so without it the report is refused rather than computed from the
    # portal's own free-text department column, which covers only people who have
    # signed in and would put a different number under the same heading.
    #
    # Kept as a file path outside the repository on purpose: it holds the name,
    # title and department of everyone in the organisation, which is not something
    # to commit. Refreshed out of band by tools/fetch_graph_tree.py.
    usage_report_org_snapshot: str = "/var/lib/rp-ai-portal/graph_tree_cache.json"
    # Optional. Identifies the handful of portal accounts that are not on the tree —
    # colleagues from other organisations, and functional accounts. Without it those
    # accounts are classified from their own portal row, which cannot tell a
    # colleague who has never signed in from a leftover test row.
    usage_report_identity_snapshot: str = ""
    # Whose reporting tree the report covers: this person and everyone under them.
    # Configurable because the post-holder changes and the id does not say who it is,
    # so a successor would otherwise have to find it in the source to understand why
    # the report still describes the previous organisation.
    usage_report_org_root_id: str = "7055e248-9d31-44a6-a44e-e06415abfff8"

    # Time zone whose midnight starts a "day" for the active-user counts. This
    # must be the *users'* zone, not the database server's: the DB runs on UTC+2
    # while the team is in UTC+8, so counting from the DB's midnight would file
    # every sign-in between 00:00 and 06:00 Beijing under the previous day.
    stats_timezone: str = "Asia/Shanghai"

    # --- RSW AI Inventory --------------------------------------------------
    # Directory where the external tool deposits the two pre-generated HTML
    # reports (full_report_browser.html / delta_report_browser.html).
    # Empty = feature disabled; the three /api/rsw/inventory/* endpoints
    # return empty/404 gracefully rather than erroring.
    rsw_inventory_dir: str = ""
    # API key for POST /api/rsw/inventory/upload (called by the external tool,
    # no JWT). Empty = upload endpoint disabled (returns 403).
    rsw_inventory_api_key: str = ""

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

    @property
    def dashboard_eid_list(self) -> list[str]:
        return [e.strip().upper() for e in self.dashboard_eids.split(",") if e.strip()]

@lru_cache
def get_settings() -> Settings:
    return Settings()
