from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_ADMIN_PASSWORD = "admin123"
DEFAULT_SECRET_KEY = "change-this-secret-before-production"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str
    database_url: str
    secret_key: str
    hash_pepper: str
    admin_username: str
    admin_password: str
    session_cookie_name: str
    session_max_age_seconds: int
    environment: str
    cookie_secure: bool
    trust_proxy_headers: bool

    def validate_for_startup(self) -> None:
        if self.environment != "production":
            return
        errors: list[str] = []
        if self.secret_key == DEFAULT_SECRET_KEY:
            errors.append("AUTH_SECRET_KEY must be changed in production")
        if self.hash_pepper in {DEFAULT_SECRET_KEY, self.secret_key}:
            errors.append("AUTH_HASH_PEPPER must be explicitly set in production")
        if self.admin_password == DEFAULT_ADMIN_PASSWORD:
            errors.append("AUTH_ADMIN_PASSWORD must be changed in production")
        if errors:
            raise RuntimeError("; ".join(errors))

    @classmethod
    def from_env(cls) -> "Settings":
        db_url = os.getenv("AUTH_DATABASE_URL")
        if not db_url:
            db_path = os.getenv("AUTH_DB_PATH", "./authorization.db")
            db_url = f"sqlite:///{db_path}"

        secret_key = os.getenv("AUTH_SECRET_KEY", DEFAULT_SECRET_KEY)
        return cls(
            app_name=os.getenv("AUTH_APP_NAME", "Small Online Authorization Server"),
            database_url=db_url,
            secret_key=secret_key,
            hash_pepper=os.getenv("AUTH_HASH_PEPPER", secret_key),
            admin_username=os.getenv("AUTH_ADMIN_USERNAME", "admin"),
            admin_password=os.getenv("AUTH_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD),
            session_cookie_name=os.getenv("AUTH_SESSION_COOKIE", "auth_admin_session"),
            session_max_age_seconds=int(os.getenv("AUTH_SESSION_MAX_AGE_SECONDS", str(7 * 24 * 60 * 60))),
            environment=os.getenv("AUTH_ENV", "development").strip().lower(),
            cookie_secure=_env_bool("AUTH_COOKIE_SECURE", False),
            trust_proxy_headers=_env_bool("AUTH_TRUST_PROXY_HEADERS", False),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
