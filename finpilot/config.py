"""Application settings, loaded from environment / `.env` via pydantic-settings."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # FINPILOT_ENV_FILE="" disables .env loading (tests stay hermetic even when a real .env exists)
    model_config = SettingsConfigDict(
        env_file=os.getenv("FINPILOT_ENV_FILE", ".env") or None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- app ---
    app_env: Literal["dev", "test", "prod"] = "dev"
    app_base_url: str = "http://localhost:8000"
    app_domain: str = "localhost"
    secret_key: SecretStr = SecretStr("change-me-dev-only")
    admin_emails: str = ""
    log_level: str = "INFO"

    # --- auth / security ---
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 14
    password_reset_ttl_min: int = 30
    password_min_length: int = 10
    rl_login_per_min_ip: int = 5
    rl_register_per_hour_ip: int = 10
    rl_reset_per_15min: int = 3
    rl_chat_per_min: int = 20  # chat messages per user per minute
    lockout_threshold: int = 5  # failed logins before the account starts locking
    lockout_base_minutes: int = 1  # lock = base * 2^(failures - threshold), capped
    lockout_max_minutes: int = 1440

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    # --- database (app connects as non-owner role; migrations as owner) ---
    database_url: str = "postgresql+psycopg://finpilot_app:finpilot_app@localhost:5432/finpilot"
    database_migration_url: str = "postgresql+psycopg://finpilot:finpilot@localhost:5432/finpilot"
    database_admin_url: str = (
        "postgresql+psycopg://finpilot_admin:finpilot_admin@localhost:5432/finpilot"
    )

    # --- redis ---
    redis_url: str = "redis://localhost:6379/0"
    # LangGraph checkpoints (Redis Search indexes) must live in DB 0: isolation is by key prefix.
    langgraph_key_prefix: str = "lg"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    celery_eager: bool = False  # run tasks inline (tests)

    # --- Mesh (the ONLY LLM provider) ---
    mesh_api_key: SecretStr | None = None
    mesh_model_fast: str = "openai/gpt-4o-mini"
    mesh_model_smart: str = "openai/gpt-4o"
    mesh_model_embed: str = "openai/text-embedding-3-small"
    mesh_timeout_s: float = 60.0
    mesh_max_retries: int = 3
    embeddings_enabled: bool | None = None  # None = auto-detect via probe

    # --- Langfuse ---
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_capture_content: bool = True
    # mask identifiers (cards, accounts, Aadhaar, PAN, phone, email, UPI…) in everything sent to
    # Mesh and Langfuse; config/privacy.yml
    pii_masking: bool = True
    langfuse_environment: str = "dev"
    langfuse_init_project_id: str = "finpilot"  # for UI deep links

    # --- currency & FX ---
    base_currency: str = "INR"
    fx_mcp_url: str = "http://localhost:8765/mcp"
    fx_source_base_url: str = "https://api.frankfurter.dev/v2"
    fx_provider: str = "ECB"
    fx_stale_after_days: int = 3
    fx_http_timeout_s: float = 10.0

    # --- delivery ---
    telegram_bot_token: SecretStr | None = None
    telegram_bot_username: str | None = None
    telegram_polling: bool = False  # dev: poll getUpdates instead of a public webhook
    telegram_link_ttl_min: int = 30
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "FinPilot <noreply@localhost>"

    # --- paths ---
    prompts_dir: Path = Field(default=ROOT_DIR / "prompts")
    config_dir: Path = Field(default=ROOT_DIR / "config")
    templates_dir: Path = Field(default=ROOT_DIR / "templates")

    @model_validator(mode="after")
    def _prod_guards(self) -> Settings:
        if self.app_env == "prod":
            key = self.secret_key.get_secret_value()
            if len(key) < 32 or key.startswith("change-me"):
                raise ValueError(
                    "SECRET_KEY must be a random value of at least 32 characters in prod"
                )
            if not self.app_base_url.startswith("https://"):
                raise ValueError("APP_BASE_URL must be https in prod (Secure cookies, HSTS)")
        return self

    @property
    def mesh_configured(self) -> bool:
        return bool(self.mesh_api_key and self.mesh_api_key.get_secret_value())

    @property
    def langfuse_configured(self) -> bool:
        return bool(self.langfuse_host and self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
