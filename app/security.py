from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import UTC, datetime

from fastapi import Response
from starlette.requests import Request

from .config import get_settings

PBKDF2_ITERATIONS = 260_000
LICENSE_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def iso_utc_z(value: datetime | None) -> str | None:
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)

    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_license_key(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


def normalize_hardware_id(value: str) -> str:
    return value.strip()


def make_license_key(groups: int = 4, group_size: int = 4) -> str:
    chunks = []
    for _ in range(groups):
        chunks.append("".join(secrets.choice(LICENSE_KEY_ALPHABET) for _ in range(group_size)))
    return "-".join(chunks)


def hash_value(value: str, *, purpose: str) -> str:
    settings = get_settings()
    payload = f"{purpose}:{settings.hash_pepper}:{value}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def hash_license_key(license_key: str) -> str:
    return hash_value(normalize_license_key(license_key), purpose="license-key")


def hash_hardware_id(hardware_id: str) -> str:
    return hash_value(normalize_hardware_id(hardware_id), purpose="hardware-id")


def display_license_key(license_key: str) -> str:
    normalized = normalize_license_key(license_key)
    if len(normalized) <= 4:
        return f"****{normalized}"
    return f"****-****-****-{normalized[-4:]}"


def display_hardware_id(hardware_id: str) -> str:
    value = normalize_hardware_id(hardware_id)
    if not value:
        return ""
    return f"hash:{hash_hardware_id(value)[:8]}"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iterations_raw, salt, expected = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
    except ValueError:
        return False

    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return hmac.compare_digest(digest, expected)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _session_signature(payload: str) -> str:
    key = get_settings().secret_key.encode("utf-8")
    return hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()


def create_session_token(admin_id: int) -> str:
    payload = _b64encode(json.dumps({"admin_id": admin_id, "iat": int(time.time())}, separators=(",", ":")).encode("utf-8"))
    signature = _session_signature(payload)
    return f"{payload}.{signature}"


def read_session_token(token: str | None) -> dict[str, int] | None:
    if not token or "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_session_signature(payload), signature):
        return None
    try:
        data = json.loads(_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None

    issued_at = int(data.get("iat", 0))
    max_age = get_settings().session_max_age_seconds
    if issued_at <= 0 or int(time.time()) - issued_at > max_age:
        return None
    return data


def set_session_cookie(response: Response, admin_id: int) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.session_cookie_name,
        create_session_token(admin_id),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(get_settings().session_cookie_name)


def create_csrf_token(session_token: str | None) -> str:
    if not session_token:
        return ""
    key = get_settings().secret_key.encode("utf-8")
    return hmac.new(key, f"csrf:{session_token}".encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf_token(session_token: str | None, csrf_token: str | None) -> bool:
    if not session_token or not csrf_token:
        return False
    return hmac.compare_digest(create_csrf_token(session_token), csrf_token)


def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for") if get_settings().trust_proxy_headers else None
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else ""
