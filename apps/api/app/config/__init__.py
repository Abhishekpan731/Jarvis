"""Runtime configuration, read from the environment and nowhere else."""

import re
from ipaddress import ip_address
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def _is_valid_hostname(hostname: str) -> bool:
    """Return whether a URL hostname is an IP address or valid DNS name."""
    try:
        ip_address(hostname)
        return True
    except ValueError:
        dns_name = hostname.removesuffix(".")
        labels = dns_name.split(".")
        return len(dns_name) <= 253 and all(
            _HOST_LABEL.fullmatch(label) is not None for label in labels
        )


def _parse_cors_origins(value: str) -> list[str]:
    """Parse specific HTTP(S) origins from the CORS_ORIGINS setting."""
    origins = [origin.strip() for origin in value.split(",")]
    if not origins or any(not origin for origin in origins):
        raise ValueError("CORS_ORIGINS must contain a comma-separated list of origins.")

    for origin in origins:
        if "*" in origin or any(character.isspace() for character in origin):
            raise ValueError("CORS_ORIGINS must contain specific origins without wildcards.")
        try:
            parsed = urlsplit(origin)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("CORS_ORIGINS contains a malformed origin.") from exc

        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "?" in origin
            or "#" in origin
            or parsed.netloc.endswith(":")
            or (port is not None and not 0 <= port <= 65535)
            or not _is_valid_hostname(hostname)
        ):
            raise ValueError("CORS_ORIGINS contains a malformed HTTP(S) origin.")
    return origins


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    cors_origins: str = "http://localhost:3000"

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, value: str) -> str:
        """Reject wildcard and malformed CORS origins at configuration load."""
        _parse_cors_origins(value)
        return value

    @property
    def cors_origin_allowlist(self) -> list[str]:
        """Return the validated, trimmed CORS origin allowlist."""
        return _parse_cors_origins(self.cors_origins)
    log_level: str = "INFO"

    # Empty disables OTLP tracing; BaseSettings reads OTEL_EXPORTER_OTLP_ENDPOINT.
    otel_exporter_otlp_endpoint: str = ""

    # API runtime role. Migrations run separately as the owner role `jarvis`.
    database_url: str = (
        "postgresql+asyncpg://jarvis_app:change-me-app@localhost:5433/jarvis"
    )
    # Required by worker_session_scope; never falls back to the API credential.
    worker_database_url: str = ""
    redis_url: str = "redis://localhost:6380/0"

    jwt_secret: str = "dev-only-secret-change-me-not-for-production"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2_592_000

    job_max_attempts: int = 3
    job_backoff_base_seconds: float = 2.0
    job_backoff_max_seconds: float = 60.0
    worker_concurrency: int = 2
    worker_per_user_max_running: int = 1
    worker_poll_interval_seconds: float = 1.0
    worker_lease_seconds: int = 60
    worker_heartbeat_seconds: int = 15
    worker_shutdown_grace_seconds: int = 20
    worker_health_port: int = 8091

    ai_provider: str = "openai"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # Retained so an existing .env using the earlier names still configures JARVIS.
    llm_api_key: str = ""
    llm_model: str = ""

    # Optional server-side speech, in addition to the browser capture/playback
    # path. "openai" is the only implemented provider; any other non-empty
    # value is reported as configured but fails with NotConfigured at call time.
    stt_provider: str = ""
    tts_provider: str = ""
    openai_stt_model: str = "gpt-4o-mini-transcribe"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "alloy"

    # OAuth. Empty client id or secret means that provider is not configured.
    # Tokens are refused rather than stored in plaintext when FERNET_KEY is empty.
    fernet_key: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    # Google redirects the browser here. The page then posts code and state to the API
    # with the user's access token. The API callback itself cannot receive that redirect.
    google_redirect_uri: str = "http://localhost:3000/"
    api_public_url: str = "http://localhost:8000"

    # Operator view. Empty means the admin route is closed. A user JWT is not enough.
    admin_token: str = ""

    @property
    def llm_configured(self) -> bool:
        return bool(self.openai_api_key or self.llm_api_key)

    @property
    def resolved_api_key(self) -> str:
        return self.openai_api_key or self.llm_api_key

    @property
    def resolved_model(self) -> str:
        return self.llm_model or self.openai_model


settings = Settings()
