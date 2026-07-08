"""Application configuration, loaded from environment / .env file."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "RP AI Portal Backend"
    debug: bool = True

    # SQLAlchemy database URL, e.g. mysql+pymysql://user:pass@host:port/db
    database_url: str

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

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
