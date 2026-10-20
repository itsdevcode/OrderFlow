from typing import ClassVar, Literal

from ipaddress import ip_network

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    audit_failure_timeout_seconds: float = Field(default=1.0, gt=0, le=5)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    app_name: str = "OrderFlow API"
    app_version: str = "1.0.0"
    debug: bool = False
    database_url: str = ""
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    payment_webhook_secret: str = ""
    payment_webhook_max_bytes: int = 65536  # 64 KB
    notification_max_attempts: int = 3
    notification_claim_lease_seconds: int = 300  # 5 minutes
    redis_url: str = "redis://localhost:6379/0"
    cache_enabled: bool = True
    cache_default_ttl_seconds: int = 300  # 5 minutes
    redis_connect_timeout: float = 2.0
    redis_socket_timeout: float = 2.0
    rate_limit_enabled: bool = True
    rate_limit_login_limit: int = Field(default=10, ge=1, le=1_000_000)
    rate_limit_login_window_seconds: int = Field(default=60, ge=1, le=86400)
    rate_limit_refresh_limit: int = Field(default=30, ge=1, le=1_000_000)
    rate_limit_refresh_window_seconds: int = Field(default=60, ge=1, le=86400)
    rate_limit_webhook_limit: int = Field(default=120, ge=1, le=1_000_000)
    rate_limit_webhook_window_seconds: int = Field(default=60, ge=1, le=86400)
    rate_limit_trusted_proxies: list[str] = Field(default_factory=list)

    @field_validator("rate_limit_trusted_proxies")
    @classmethod
    def validate_trusted_proxies(cls, values: list[str]) -> list[str]:
        return [str(ip_network(value)) for value in values]

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_secrets(self) -> "Settings":
        # In development/debug mode, provide a fallback if not configured
        if not self.payment_webhook_secret:
            if self.debug:
                self.payment_webhook_secret = "dev_insecure_webhook_secret_key_change_in_production"
            else:
                raise ValueError(
                    "PAYMENT_WEBHOOK_SECRET must be explicitly configured in non-debug/production environments"
                )
        return self


settings = Settings()