from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """API settings. All configuration comes from environment variables (or a local .env)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str
    postgres_port: int = 5432

    db_echo: bool = False

    redis_url: str

    # Bearer token for /admin endpoints.
    admin_token: SecretStr
    # How long a valid API key lookup stays cached in Redis.
    api_key_cache_ttl_seconds: int = 60

    @field_validator("admin_token")
    @classmethod
    def _admin_token_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 16:
            raise ValueError("ADMIN_TOKEN must be at least 16 characters")
        return value

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


class WorkerSettings(Settings):
    """Extra settings only the email worker needs (the API never loads these)."""

    smtp_host: str
    smtp_port: int = 1025
    smtp_from: str
    smtp_timeout_seconds: float = Field(default=10, gt=0)

    # Fake a real email provider: random latency up to this many ms...
    smtp_delay_ms: int = Field(default=50, ge=0)
    # ...and this share of sends fail on purpose (0.0 - 1.0).
    smtp_fail_rate: float = Field(default=0.02, ge=0, le=1)

    worker_concurrency: int = Field(default=20, ge=1)
    worker_max_attempts: int = Field(default=5, ge=1)
    # Retry n waits base * 2^(n-1) seconds (plus jitter), capped at worker_retry_max_seconds.
    worker_retry_base_seconds: float = Field(default=1, gt=0)
    worker_retry_max_seconds: float = Field(default=300, gt=0)
    # On SIGTERM, how long to wait for in-flight jobs before giving up.
    worker_shutdown_timeout_seconds: float = Field(default=25, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
