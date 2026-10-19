from typing import ClassVar

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
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