from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


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

    @classmethod
    def from_env(cls) -> "Settings":
        db_url = os.getenv("AUTH_DATABASE_URL")
        if not db_url:
            db_path = os.getenv("AUTH_DB_PATH", "./authorization.db")
            db_url = f"sqlite:///{db_path}"

        secret_key = os.getenv("AUTH_SECRET_KEY", "change-this-secret-before-production")
        return cls(
            app_name=os.getenv("AUTH_APP_NAME", "Small Online Authorization Server"),
            database_url=db_url,
            secret_key=secret_key,
            hash_pepper=os.getenv("AUTH_HASH_PEPPER", secret_key),
            admin_username=os.getenv("AUTH_ADMIN_USERNAME", "admin"),
            admin_password=os.getenv("AUTH_ADMIN_PASSWORD", "admin123"),
            session_cookie_name=os.getenv("AUTH_SESSION_COOKIE", "auth_admin_session"),
            session_max_age_seconds=int(os.getenv("AUTH_SESSION_MAX_AGE_SECONDS", str(7 * 24 * 60 * 60))),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
