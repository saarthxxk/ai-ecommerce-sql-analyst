"""Application settings, loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    database_url: str = (
        "postgresql+psycopg://analyst_ro:analyst_ro_pw@localhost:5432/ecommerce_analyst"
    )
    admin_database_url: str = (
        "postgresql+psycopg://analyst_owner:analyst_owner_pw@localhost:5432/ecommerce_analyst"
    )

    # Bootstrap-only superuser credentials (used by scripts/load_data.py --bootstrap).
    pg_superuser: str = "postgres"
    pg_superuser_password: str = ""
    pg_host: str = "localhost"
    pg_port: int = 5432
    analyst_owner_password: str = "analyst_owner_pw"
    analyst_ro_password: str = "analyst_ro_pw"

    # --- LLM ---
    llm_provider: str = "gemini"
    llm_temperature: float = 0.0

    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-lite-latest"

    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # --- Behaviour ---
    data_dir: str = "ecommerce_dataset"
    max_result_rows: int = 500
    statement_timeout_ms: int = 15_000

    @property
    def database_name(self) -> str:
        return self.admin_database_url.rsplit("/", 1)[-1].split("?")[0]


@lru_cache
def get_settings() -> Settings:
    return Settings()
