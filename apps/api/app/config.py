"""Runtime configuration, read from the environment and nowhere else."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://jarvis:jarvis@localhost:5433/jarvis"
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
