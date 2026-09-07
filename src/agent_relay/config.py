"""Configuration, loaded from the environment (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_FIELDS = frozenset({"api_token", "slack_webhook_url", "github_token"})


class Settings(BaseSettings):
    """Runtime configuration.

    Every field is optional; the relay runs with zero configuration using a local
    SQLite file, no auth, no Slack and no GitHub.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
    )

    # --- server ---
    host: str = Field(default="127.0.0.1", alias="AGENT_RELAY_HOST")
    port: int = Field(default=8077, alias="AGENT_RELAY_PORT")
    db_url: str = Field(default="sqlite:///./data/agent_relay.db", alias="AGENT_RELAY_DB_URL")
    log_level: str = Field(default="INFO", alias="AGENT_RELAY_LOG_LEVEL")

    # --- optional shared auth ---
    api_token: str | None = Field(default=None, alias="AGENT_RELAY_API_TOKEN")

    # --- slack ---
    slack_webhook_url: str | None = Field(default=None, alias="SLACK_WEBHOOK_URL")
    slack_event_types: str | None = Field(default=None, alias="SLACK_EVENT_TYPES")
    slack_timeout_seconds: float = Field(default=5.0, alias="SLACK_TIMEOUT_SECONDS")

    # --- github ---
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    github_owner: str | None = Field(default=None, alias="GITHUB_OWNER")
    github_repo: str | None = Field(default=None, alias="GITHUB_REPO")
    github_task_prefix: str = Field(default="GH-", alias="GITHUB_TASK_PREFIX")

    # --- context/coordination tuning ---
    context_recent_limit: int = Field(default=10, alias="AGENT_RELAY_CONTEXT_LIMIT")
    context_window_hours: int = Field(default=72, alias="AGENT_RELAY_CONTEXT_WINDOW_HOURS")

    @field_validator(
        "api_token",
        "slack_webhook_url",
        "github_token",
        "github_owner",
        "github_repo",
        "slack_event_types",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        """Treat `FOO=` in a .env file as "not configured" rather than an empty string."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_webhook_url)

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_owner and self.github_repo)

    @property
    def slack_event_type_filter(self) -> frozenset[str] | None:
        """Event types to forward to Slack, or None meaning "all"."""
        if not self.slack_event_types:
            return None
        return frozenset(t.strip().upper() for t in self.slack_event_types.split(",") if t.strip())

    def public_summary(self) -> dict[str, object]:
        """Non-secret view of the configuration, safe to log or return over HTTP."""
        return {
            "db_url": redact_db_url(self.db_url),
            "auth_required": self.api_token is not None,
            "slack_enabled": self.slack_enabled,
            "github_enabled": self.github_enabled,
            "github_repo": (
                f"{self.github_owner}/{self.github_repo}" if self.github_enabled else None
            ),
        }


def redact_db_url(url: str) -> str:
    """Strip credentials out of a database URL before it is logged."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
