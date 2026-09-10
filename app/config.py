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

    # --- Gerrit: the RSW / RA shared asset repository -----------------------
    # radio/ai-assets on gerrit-sigma holds skill/, agent/ and tool/ assets,
    # each a directory with a manifest.yaml. The portal is a read-only consumer.
    #
    # Credentials are a Gerrit HTTP password (Settings -> HTTP Credentials),
    # NOT a domain password, and must belong to a service account: a personal
    # one breaks when that person rotates it, and files every read in the
    # directory under their name. The SSH port (29418) accepts publickey only,
    # so HTTPS Basic is the only option here.
    gerrit_base_url: str = "https://gerrit-sigma.gic.ericsson.se"
    gerrit_http_user: str = ""
    gerrit_http_password: str = ""
    gerrit_assets_project: str = "radio/ai-assets"
    gerrit_assets_branch: str = "master"
    # Same trade-off as ldap_verify_tls: the internal chain is incomplete and
    # verifying would take the whole feature down.
    gerrit_verify_tls: bool = False
    # Bearer token guarding POST /api/integrations/gerrit/ai-assets/sync.
    # Empty = endpoint disabled, same shape as skill_import_token.
    gerrit_sync_token: str = ""
    # Where an asset lands when its manifest ``owner`` resolves to nobody in the
    # directory. A team name ("radio-ai-platform") or a person the directory has
    # no entry for still has to be attributed to somebody, or the asset falls out
    # of every department count. Their manager is the honest answer; the card
    # still shows the manifest's own owner text as the author.
    gerrit_owner_fallback_eid: str = "ECUIDEN"

    # --- IVC / RVC 用量推送 ---------------------------------------------------
    # 守 POST /api/integrations/ivc/usage/events 的 bearer token。对方的系统用它把下载 /
    # 点赞事件推过来。为空 = 端点关闭(答 503 并点名缺哪个变量),形状与 gerrit_sync_token 一致。
    #
    # **为什么是一个静态 token 而不是一个门户账号。** 调用方是对方机房里的一个无人值守服务:
    # 走门户的 JWT 就得给它一个 LDAP 账号并让它交互登录,而那个账号一旦存在就能做门户里的
    # 每一件事 —— 而它只需要往一张流水表里追加行。这个 token 的作用域正好是那一个端点。
    #
    # 轮换:改这个变量并重启,然后把新值给对方。没有第二个受影响的地方 —— 它不签任何东西,
    # 也不在浏览器里出现过。
    ivc_push_token: str = ""

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
    # 给门户自己的提交签名用的 SSH 密钥。空 = 不签名(历史行为)。
    #
    # **为什么需要它:** 上传包的目标组开着 GitLab 的「Reject unsigned commits」推送规则,
    # 而门户是 git init/commit/push 发布的,提交默认没有签名 —— 实测被 pre-receive 钩子
    # 拒收(`Commit must be signed with a GPG key`),包已解开、项目已建好,倒在最后一步。
    #
    # **为什么是 SSH 而不是 GPG:** 镜像里没有 gpg(实测 `command -v gpg` 为空),但有
    # openssh-client 自带的 ssh-keygen;而这个实例的那条规则实测接受 SSH 签名(本仓自己的
    # 提交就是这么签的)。
    #
    # 值填**公钥**路径。私钥必须在同一目录、同名去掉 `.pub` —— `ssh-keygen -Y sign` 就是
    # 这么找它的。密钥不能有 passphrase:容器里没有 ssh-agent 可以解锁它。
    git_signing_key: str = ""
    # Max upload artifact size in bytes (default 50 MB).
    #
    # **这一项现在只管 skill 上传。** tool/agent 的包走下面那两项 —— 它们是两类东西:skill 是
    # 一份技能定义(文本为主),包是一个完整的项目(可能带模型、样例数据、依赖)。原来两边共用
    # 这一个数字,于是给包放宽也会连带把 skill 放宽,而没人要求那件事。
    max_upload_bytes: int = 50 * 1024 * 1024

    # --- tool/agent 包上传的两个上限 ------------------------------------------
    #
    # 分成两个数字,因为它们卡的是不同的东西:
    #
    # `max_package_upload_bytes` 卡**上传上来的字节**(压缩包本身)。
    # `max_package_extract_bytes` 卡**解压之后的总字节**,这是 zip bomb 的预算。
    #
    # 后者必须 >= 前者,否则一个刚好合规的压缩包会在解压时被自己的上限拒掉 —— 而报错说的是
    # "解压后的总大小超过 N MB",与用户看到的"我这个包只有 190MB"对不上。原来两处读同一个
    # 常量,正好踩在这个坑的边上:50MB 的包解压后超过 50MB 就被拒。
    #
    # 取 2 倍是给"文本/代码为主、压缩比高"的包留出余量,同时仍然给磁盘一个明确的上界。
    #
    # 运维上要知道的两件事(生产机 100.77.162.66):
    #   * 上传路径是 `await file.read()`,整个 body 进内存,而这个后端容器**没有内存上限**
    #     (docker inspect Memory=0)。并发几个 200MB 的上传就是几百 MB 的常驻内存。
    #   * 解压落在容器的 /tmp,也就是宿主机的 `/`,那个盘现在 79% 满(125G 用了 93G)。
    #     单次上传峰值约 上传字节 + 解压字节;真要长期跑在 200MB 这个量级,得先给这台机器
    #     补上磁盘清理(它至今没有),否则一次并发上传就可能把盘写满。
    max_package_upload_bytes: int = 200 * 1024 * 1024
    max_package_extract_bytes: int = 400 * 1024 * 1024

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
    # EBAOXUE Baosheng Xue. Gates the department usage report, which names
    # individuals — adding someone here shows them per-person output, not just
    # a sync button. That is the reason this list is short.
    #
    # ECUIDEN (Tracy Deng) 曾在这张名单上,2026-09-02 按操作者要求移除。加她进来的理由是
    # 她需要 RSW 的同步页(她拥有那个页面读取的 Gerrit `radio/ai-assets` 仓),而这张名单
    # **同时**守着上面那份逐人报告 —— 一张名单管两件权限面差得很远的事,所以给她一个同步
    # 按钮的同时也给了她每个人的姓名和用量。
    #
    # 移除她因此也一并收走了 RSW 同步页。这是那次决定的已知代价,不是疏漏。真正的解法是把
    # 这张名单拆成两个(一个窄的「能跑同步」名单 + 现在这个「能看逐人报告」名单),那是一次
    # 独立改动;在拆之前,任何"只想给某人同步权限"的请求都会连带给出逐人报告。
    #
    # **生产没有设 DASHBOARD_EIDS 环境变量**(查过容器 env),所以这里的默认值就是线上实际
    # 生效的名单 —— 改它才真的生效,前端 dashboard-access.ts 那份只决定显不显示入口。
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

    # 接口报错诊断用的 LLM。默认指向生产上已有的判定器容器 —— 它讲 OpenAI 兼容协议，只在
    # compose 网络内可达（没有发布端口），所以这条调用不出机房、也不需要额外凭据。
    # 换成别的 provider 只要改这两个值，代码一行不用动。
    error_diagnosis_llm_base: str = "http://audit-judge:8790/v1"
    error_diagnosis_model: str = "claude-sonnet-5"
    # 单次调用上限。判定器历史上出现过 200s 级慢响应，而这跑在 reconciler 的循环里，
    # 不设上限会让一次 sweep 无限期挂住。
    error_diagnosis_timeout: int = 90
    # 每轮 sweep 最多诊断几条。见 services/api_errors.py::diagnose_pending。
    # 默认 1 而不是 2：稳态下每天不到一条新指纹（实测 13 天 8 个），2 和 1 没区别；有区别的
    # 只有异常涌入那一天，而那天恰恰最不该跟技能审计抢判定器。
    error_diagnosis_batch: int = 1
    # 待诊断超过这个数就认为在「涌入」，这一轮只做 1 条。见 diagnose_pending 里的理由。
    error_diagnosis_flood_threshold: int = 10

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
