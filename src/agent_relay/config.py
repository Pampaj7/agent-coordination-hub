"""Configuration, loaded from the environment (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_FIELDS = frozenset(
    {
        "api_token",
        "slack_webhook_url",
        "slack_bot_token",
        "slack_signing_secret",
        "github_token",
        "github_webhook_secret",
        "anthropic_api_key",
        "anthropic_auth_token",
    }
)


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

    # --- slack (outbound) ---
    slack_webhook_url: str | None = Field(default=None, alias="SLACK_WEBHOOK_URL")
    slack_event_types: str | None = Field(default=None, alias="SLACK_EVENT_TYPES")
    slack_timeout_seconds: float = Field(default=5.0, alias="SLACK_TIMEOUT_SECONDS")

    # --- slack (bidirectional bot; V2) ---
    #: A bot token is what makes Slack two-way: chat.postMessage returns the message
    #: ts, which is how a human's threaded reply finds its way back to an event.
    slack_bot_token: str | None = Field(default=None, alias="SLACK_BOT_TOKEN")
    slack_signing_secret: str | None = Field(default=None, alias="SLACK_SIGNING_SECRET")
    slack_default_channel: str | None = Field(default=None, alias="SLACK_DEFAULT_CHANNEL")
    #: "tether=C012AB,drends=C034CD" — per-project channel routing.
    slack_channel_map: str | None = Field(default=None, alias="SLACK_CHANNEL_MAP")

    # --- presence and claim hygiene (V2) ---
    heartbeat_online_seconds: int = Field(default=300, alias="AGENT_RELAY_HEARTBEAT_ONLINE_SECONDS")
    heartbeat_idle_seconds: int = Field(default=1800, alias="AGENT_RELAY_HEARTBEAT_IDLE_SECONDS")
    claim_stale_hours: float = Field(default=24.0, alias="AGENT_RELAY_CLAIM_STALE_HOURS")
    #: 0 disables auto-release entirely; a stale claim is then only ever reported.
    claim_expiry_hours: float = Field(default=0.0, alias="AGENT_RELAY_CLAIM_EXPIRY_HOURS")
    sweeper_interval_seconds: int = Field(default=300, alias="AGENT_RELAY_SWEEPER_INTERVAL_SECONDS")

    # --- github ---
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    github_owner: str | None = Field(default=None, alias="GITHUB_OWNER")
    github_repo: str | None = Field(default=None, alias="GITHUB_REPO")
    github_task_prefix: str = Field(default="GH-", alias="GITHUB_TASK_PREFIX")

    # --- github ingestion (V2) ---
    github_webhook_secret: str | None = Field(default=None, alias="GITHUB_WEBHOOK_SECRET")
    #: Project name that ingested GitHub activity is filed under.
    github_ingest_project: str | None = Field(default=None, alias="GITHUB_INGEST_PROJECT")
    #: 0 disables polling. Use it when the relay has no publicly reachable URL.
    github_poll_interval_seconds: int = Field(default=0, alias="GITHUB_POLL_INTERVAL_SECONDS")

    # --- coordinator LLM (V2) ---
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    #: The SDK also accepts a bearer token, and resolves an `ant auth login` profile
    #: from disk when neither is set. Recognising those means a team with Console
    #: access but no long-lived key is not shut out.
    anthropic_auth_token: str | None = Field(default=None, alias="ANTHROPIC_AUTH_TOKEN")
    #: Force the coordinator on when the credential lives somewhere this process
    #: cannot see from the environment (an OAuth profile on disk, for instance).
    coordinator_force: bool = Field(default=False, alias="AGENT_RELAY_COORDINATOR")
    coordinator_model: str = Field(default="claude-opus-5", alias="COORDINATOR_MODEL")
    coordinator_max_tokens: int = Field(default=16000, alias="COORDINATOR_MAX_TOKENS")
    #: low | medium | high | xhigh | max. A status brief is a small job; "low" keeps
    #: the hourly cost negligible without hurting the output.
    coordinator_effort: str = Field(default="low", alias="COORDINATOR_EFFORT")
    #: 0 disables the scheduled team status post.
    status_interval_minutes: int = Field(default=0, alias="AGENT_RELAY_STATUS_INTERVAL_MINUTES")
    status_projects: str | None = Field(default=None, alias="AGENT_RELAY_STATUS_PROJECTS")

    # --- experiment trackers (V2, link-only) ---
    wandb_entity: str | None = Field(default=None, alias="WANDB_ENTITY")
    wandb_project: str | None = Field(default=None, alias="WANDB_PROJECT")
    mlflow_tracking_uri: str | None = Field(default=None, alias="MLFLOW_TRACKING_URI")

    # --- dashboard (V2) ---
    dashboard_enabled: bool = Field(default=True, alias="AGENT_RELAY_DASHBOARD")
    public_base_url: str | None = Field(default=None, alias="AGENT_RELAY_PUBLIC_URL")

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
        "slack_bot_token",
        "slack_signing_secret",
        "slack_default_channel",
        "slack_channel_map",
        "github_webhook_secret",
        "github_ingest_project",
        "anthropic_api_key",
        "status_projects",
        "wandb_entity",
        "wandb_project",
        "mlflow_tracking_uri",
        "public_base_url",
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
    def slack_bot_enabled(self) -> bool:
        """Two-way Slack needs a bot token; a webhook alone can only post."""
        return bool(self.slack_bot_token)

    @property
    def slack_events_enabled(self) -> bool:
        """Inbound Slack events additionally need the signing secret to be verifiable."""
        return bool(self.slack_bot_token and self.slack_signing_secret)

    @property
    def slack_channels(self) -> dict[str, str]:
        """Parse SLACK_CHANNEL_MAP ("tether=C012AB,drends=C034CD")."""
        mapping: dict[str, str] = {}
        for pair in (self.slack_channel_map or "").split(","):
            project, sep, channel = pair.partition("=")
            if sep and project.strip() and channel.strip():
                mapping[project.strip()] = channel.strip()
        return mapping

    def channel_for(self, project: str | None) -> str | None:
        """Per-project channel, falling back to the default channel."""
        if project and (channel := self.slack_channels.get(project)):
            return channel
        return self.slack_default_channel

    @property
    def github_webhooks_enabled(self) -> bool:
        return bool(self.github_webhook_secret)

    @property
    def github_polling_enabled(self) -> bool:
        return self.github_enabled and self.github_poll_interval_seconds > 0

    @property
    def coordinator_enabled(self) -> bool:
        """Whether to attempt an LLM briefing at all.

        A Claude *subscription* is not API access, so most teams will have none of
        these and get the deterministic briefing — which is the designed outcome, not
        a degraded one.
        """
        return bool(self.anthropic_api_key or self.anthropic_auth_token or self.coordinator_force)

    @property
    def status_project_list(self) -> list[str]:
        return [p.strip() for p in (self.status_projects or "").split(",") if p.strip()]

    @property
    def auto_release_enabled(self) -> bool:
        return self.claim_expiry_hours > 0

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
            "slack_bot": self.slack_bot_enabled,
            "slack_events": self.slack_events_enabled,
            "github_webhooks": self.github_webhooks_enabled,
            "github_polling": self.github_polling_enabled,
            "coordinator_llm": self.coordinator_enabled,
            "auto_release": self.auto_release_enabled,
            "dashboard": self.dashboard_enabled,
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
