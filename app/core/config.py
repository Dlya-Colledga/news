from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, model_validator


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://frame:frame@localhost:5432/frame"
    secret_key: str = "development-only-change-this-secret-key"
    base_url: str = "http://localhost:8000"
    environment: str = "development"
    cookie_secure: bool = False
    ingest_interval_minutes: int = Field(15, ge=1)
    http_timeout: float = Field(20, gt=0)
    request_interval_seconds: float = Field(3, ge=0)
    dedup_auto_threshold: float = Field(93, ge=0, le=100)
    dedup_review_threshold: float = Field(76, ge=0, le=100)
    dedup_window_days: int = Field(7, ge=1)
    semantic_enabled: bool = False
    semantic_model_path: str = ""
    tmdb_api_key: str = ""
    page_size: int = Field(12, ge=1, le=100)
    user_agent: str = "FrameNews/0.1 (+local research RSS reader)"

    @model_validator(mode="after")
    def secure_production(self):
        if self.dedup_review_threshold > self.dedup_auto_threshold:
            raise ValueError("Review threshold must not exceed automatic threshold")
        if self.environment == "production" and (
            len(self.secret_key) < 32
            or self.secret_key.startswith(("development-", "replace-"))
            or not self.cookie_secure
            or not self.base_url.startswith("https://")
        ):
            raise ValueError("Production requires a random secret, HTTPS base URL and secure cookies")
        return self


@lru_cache
def get_settings():
    return Settings()
