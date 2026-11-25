from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env)."""

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
