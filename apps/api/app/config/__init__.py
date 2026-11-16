"""Runtime configuration, read from the environment and nowhere else."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    # Runtime connection. Must be the non-owner `jarvis_app` role so Postgres
    # RLS is actually evaluated (table owners bypass RLS unless FORCED, but a
    # non-owner role is subject to it regardless). Migrations run separately
    # as the owner role `jarvis` via DATABASE_URL_SYNC / alembic.ini.
    database_url: str = (
        "postgresql+asyncpg://jarvis_app:change-me-app@localhost:5433/jarvis"
    )
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

    # Optional server-side speech. Empty means the browser captures and speaks,
    # and the API reports speech as not configured rather than inventing audio.
    stt_provider: str = ""
    tts_provider: str = ""

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
