from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://lockstep:lockstep@localhost:5432/lockstep"
    redis_url: str = "redis://localhost:6379/0"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    ai_concurrency: int = 5

    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    storage_backend: str = "local"
    upload_dir: str = "./data/uploads"

    api_v1_prefix: str = "/api/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
