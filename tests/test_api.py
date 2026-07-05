from __future__ import annotations

import importlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR_RAW = str(ROOT_DIR)
sys.path = [item for item in sys.path if item != ROOT_DIR_RAW]
sys.path.insert(0, ROOT_DIR_RAW)


def _fresh_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "test_auth.db"))
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-secret")
    monkeypatch.setenv("AUTH_HASH_PEPPER", "test-pepper")
    monkeypatch.setenv("AUTH_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("AUTH_ADMIN_PASSWORD", "admin123")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    module = importlib.import_module("app.main")
    return module.app


def _csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf token not found"
    return match.group(1)


def _login_admin(client) -> str:
    login = client.post(
        "/admin/login",
        data={"username": "admin", "password": "admin123", "next": "/admin/licenses"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    page = client.get("/admin/licenses/new")
    assert page.status_code == 200
    return _csrf_token(page.text)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    app = _fresh_app(tmp_path, monkeypatch)
    with TestClient(app) as test_client:
        yield test_client


def _create_license(days: int = 30):
    from app.database import SessionLocal
    from app.services import create_license

    with SessionLocal() as db:
        license, key = create_license(db, customer_name="测试客户", duration_days=days, is_permanent=False, note="pytest")
        return license.id, key


def _create_admin_license(client, customer_name: str = "后台客户") -> tuple[int, str]:
    csrf_token = _login_admin(client)
    response = client.post(
        "/admin/licenses/new",
        data={"csrf_token": csrf_token, "customer_name": customer_name, "duration_days": "30", "note": "后台创建"},
    )
    assert response.status_code == 200
    key_match = re.search(r'<div class="license-key">([^<]+)</div>', response.text)
    assert key_match, response.text
    detail_match = re.search(r'/admin/licenses/(\d+)', response.text)
    assert detail_match, response.text
    return int(detail_match.group(1)), key_match.group(1)


def _assert_iso_utc_z(value: str | None) -> None:
    assert isinstance(value, str)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value), value


def test_activate_verify_and_heartbeat(client):
    license_id, key = _create_license(days=30)

    activation = client.post(
        "/api/v1/activate",
        json={"license_key": key, "hardware_id": "HW-001", "client_version": "1.0.0"},
    )
    assert activation.status_code == 200
    body = activation.json()
    assert body["success"] is True
    assert body["valid"] is True
    assert body["code"] == "ok"
    assert body["license_id"] == license_id
    assert body["status"] == "active"
    assert body["remaining_days"] >= 1
    _assert_iso_utc_z(body["server_time"])
    _assert_iso_utc_z(body["expire_at"])

    from app.database import SessionLocal
    from app.models import License

    with SessionLocal() as db:
        license = db.get(License, license_id)
        assert license.hardware_id_display.startswith("hash:")
        assert "HW-001" not in license.hardware_id_display

    verify = client.post(
        "/api/v1/verify",
        json={"license_id": license_id, "hardware_id": "HW-001", "client_version": "1.0.0"},
    )
    assert verify.status_code == 200
    verify_body = verify.json()
    assert verify_body["success"] is True
    assert verify_body["code"] == "ok"
    _assert_iso_utc_z(verify_body["server_time"])

    heartbeat = client.post(
        "/api/v1/heartbeat",
        json={"license_key": key, "hardware_id": "HW-001", "client_version": "1.0.0"},
    )
    assert heartbeat.status_code == 200
    heartbeat_body = heartbeat.json()
    assert heartbeat_body["success"] is True
    assert heartbeat_body["code"] == "ok"
    _assert_iso_utc_z(heartbeat_body["server_time"])

    second_activation = client.post("/api/v1/activate", json={"license_key": key, "hardware_id": "HW-002"})
    assert second_activation.status_code == 200
    assert second_activation.json()["success"] is False
    assert second_activation.json()["code"] == "already_used"
    assert second_activation.json()["message"] == "卡密已被使用"


def test_concurrent_activation_only_binds_once(client):
    from app.database import SessionLocal
    from app.models import License
    from app.services import activate_license

    license_id, key = _create_license(days=30)
    barrier = Barrier(2)

    def activate(hardware_id: str):
        barrier.wait(timeout=5)
        with SessionLocal() as db:
            result = activate_license(db, license_key=key, hardware_id=hardware_id, client_version="pytest", ip="127.0.0.1")
            return result.success, result.message, result.license.hardware_id_display if result.license else None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(activate, ["HW-CONCURRENT-A", "HW-CONCURRENT-B"]))

    assert sum(1 for success, _, _ in results if success) == 1
    assert sum(1 for success, message, _ in results if not success and message == "卡密已被使用") == 1

    with SessionLocal() as db:
        license = db.get(License, license_id)
        assert license.hardware_id_hash is not None
        assert license.hardware_id_display in {display for _, _, display in results if display}


def test_hardware_mismatch_is_rejected(client):
    _, key = _create_license(days=30)
    client.post("/api/v1/activate", json={"license_key": key, "hardware_id": "HW-A"})

    response = client.post("/api/v1/verify", json={"license_key": key, "hardware_id": "HW-B"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "hardware_mismatch"
    assert body["message"] == "硬件不匹配"
    _assert_iso_utc_z(body["server_time"])


def test_expired_license_is_rejected(client):
    from app.database import SessionLocal
    from app.models import License
    from app.security import utcnow

    license_id, key = _create_license(days=1)
    client.post("/api/v1/activate", json={"license_key": key, "hardware_id": "HW-EXPIRED"})

    with SessionLocal() as db:
        license = db.get(License, license_id)
        license.expire_at = utcnow() - timedelta(minutes=1)
        db.commit()

    response = client.post("/api/v1/verify", json={"license_key": key, "hardware_id": "HW-EXPIRED"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "expired"
    assert body["status"] == "expired"
    assert body["message"] == "授权已过期"


def test_unbind_allows_reactivation_on_new_hardware(client):
    from app.database import SessionLocal
    from app.models import License
    from app.services import unbind_license

    license_id, key = _create_license(days=30)
    client.post("/api/v1/activate", json={"license_key": key, "hardware_id": "HW-OLD"})

    with SessionLocal() as db:
        license = db.get(License, license_id)
        unbind_license(db, license)

    old_verify = client.post("/api/v1/verify", json={"license_key": key, "hardware_id": "HW-OLD"})
    assert old_verify.status_code == 200
    old_verify_body = old_verify.json()
    assert old_verify_body["success"] is False
    assert old_verify_body["valid"] is False
    assert old_verify_body["code"] == "not_activated"
    assert old_verify_body["message"] == "授权未激活"
    _assert_iso_utc_z(old_verify_body["server_time"])

    new_verify = client.post("/api/v1/verify", json={"license_key": key, "hardware_id": "HW-NEW"})
    assert new_verify.status_code == 200
    new_verify_body = new_verify.json()
    assert new_verify_body["success"] is False
    assert new_verify_body["valid"] is False
    assert new_verify_body["code"] == "not_activated"
    assert new_verify_body["message"] == "授权未激活"
    _assert_iso_utc_z(new_verify_body["server_time"])

    response = client.post("/api/v1/activate", json={"license_key": key, "hardware_id": "HW-NEW"})

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["code"] == "ok"


def test_cancel_permanent_sets_explicit_expire_at(client):
    from app.database import SessionLocal
    from app.models import License
    from app.services import create_license, set_permanent

    with SessionLocal() as db:
        license, key = create_license(db, customer_name="永久客户", duration_days=15, is_permanent=True, note="pytest")
        license_id = license.id

    activation = client.post("/api/v1/activate", json={"license_key": key, "hardware_id": "HW-PERM"})
    assert activation.status_code == 200
    assert activation.json()["success"] is True
    assert activation.json()["code"] == "ok"
    assert activation.json()["is_permanent"] is True
    assert activation.json()["expire_at"] is None
    _assert_iso_utc_z(activation.json()["server_time"])

    with SessionLocal() as db:
        license = db.get(License, license_id)
        set_permanent(db, license, False)

    verify = client.post("/api/v1/verify", json={"license_key": key, "hardware_id": "HW-PERM"})
    assert verify.status_code == 200
    body = verify.json()
    assert body["success"] is True
    assert body["code"] == "ok"
    assert body["is_permanent"] is False
    assert body["expire_at"] is not None
    _assert_iso_utc_z(body["expire_at"])
    _assert_iso_utc_z(body["server_time"])
    assert body["remaining_days"] >= 1


def test_admin_login_and_create_license_page(client):
    license_id, key = _create_admin_license(client, "后台客户")
    response = client.get(f"/admin/licenses/{license_id}")

    assert response.status_code == 200
    assert "后台客户" in response.text

    list_page = client.get("/admin/licenses")
    assert list_page.status_code == 200
    assert "已激活" in list_page.text
    assert "续期30天" in list_page.text

    logs_page = client.get("/admin/logs")
    assert logs_page.status_code == 200
    assert "授权日志" in logs_page.text
    assert "create" in logs_page.text

    filtered_logs_page = client.get("/admin/logs?event_type=create&result=success")
    assert filtered_logs_page.status_code == 200
    assert "后台客户" in filtered_logs_page.text


def test_delete_unused_license_is_logical_and_keeps_logs(client):
    from app.database import SessionLocal
    from app.models import License, LicenseLog

    license_id, key = _create_admin_license(client, "待删除客户")
    detail_page = client.get(f"/admin/licenses/{license_id}")
    csrf_token = _csrf_token(detail_page.text)

    delete_response = client.post(f"/admin/licenses/{license_id}/delete", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert delete_response.status_code == 303

    with SessionLocal() as db:
        license = db.get(License, license_id)
        assert license is not None
        assert license.status == "deleted"
        assert db.query(LicenseLog).filter(LicenseLog.license_id == license_id).count() >= 2

    activation = client.post("/api/v1/activate", json={"license_key": key, "hardware_id": "HW-DELETED"})
    assert activation.status_code == 200
    assert activation.json()["success"] is False
    assert activation.json()["code"] == "deleted"
    assert activation.json()["message"] == "授权已删除"


def test_delete_active_license_from_list_blocks_verify(client):
    from app.database import SessionLocal
    from app.models import License, LicenseLog

    license_id, key = _create_admin_license(client, "已激活待删除客户")
    activation = client.post("/api/v1/activate", json={"license_key": key, "hardware_id": "HW-ACTIVE-DELETE"})
    assert activation.status_code == 200
    assert activation.json()["success"] is True

    list_page = client.get("/admin/licenses")
    assert list_page.status_code == 200
    assert f"/admin/licenses/{license_id}/delete" in list_page.text
    csrf_token = _csrf_token(list_page.text)

    delete_response = client.post(f"/admin/licenses/{license_id}/delete", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert delete_response.status_code == 303

    with SessionLocal() as db:
        license = db.get(License, license_id)
        assert license is not None
        assert license.status == "deleted"
        assert license.activated_at is not None
        assert db.query(LicenseLog).filter(LicenseLog.license_id == license_id, LicenseLog.event_type == "delete").count() == 1

    verify = client.post("/api/v1/verify", json={"license_key": key, "hardware_id": "HW-ACTIVE-DELETE"})
    assert verify.status_code == 200
    body = verify.json()
    assert body["success"] is False
    assert body["valid"] is False
    assert body["code"] == "deleted"
    assert body["message"] == "授权已删除"
    _assert_iso_utc_z(body["server_time"])


def test_deleted_license_cannot_be_restored_or_modified(client):
    from app.database import SessionLocal
    from app.models import License

    license_id, key = _create_admin_license(client, "删除终态客户")
    detail_page = client.get(f"/admin/licenses/{license_id}")
    csrf_token = _csrf_token(detail_page.text)

    delete_response = client.post(f"/admin/licenses/{license_id}/delete", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert delete_response.status_code == 303

    for suffix, data in [
        ("restore", {"csrf_token": csrf_token}),
        ("disable", {"csrf_token": csrf_token}),
        ("renew", {"csrf_token": csrf_token, "days": "30"}),
        ("permanent", {"csrf_token": csrf_token, "value": "true"}),
        ("unbind", {"csrf_token": csrf_token}),
    ]:
        response = client.post(f"/admin/licenses/{license_id}/{suffix}", data=data, follow_redirects=False)
        assert response.status_code == 400
        assert "授权已删除" in response.text
        with SessionLocal() as db:
            license = db.get(License, license_id)
            assert license.status == "deleted"

    activation = client.post("/api/v1/activate", json={"license_key": key, "hardware_id": "HW-DELETED-TERM"})
    assert activation.status_code == 200
    assert activation.json()["success"] is False
    assert activation.json()["code"] == "deleted"
    assert activation.json()["message"] == "授权已删除"


def test_missing_license_identifier_returns_protocol_error(client):
    response = client.post("/api/v1/verify", json={"hardware_id": "HW-NO-LICENSE"})

    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["valid"] is False
    assert body["code"] == "missing_license_identifier"
    assert body["message"] == "license_key 或 license_id 必须提供一个"
    _assert_iso_utc_z(body["server_time"])


def test_api_validation_error_uses_protocol_shape(client):
    response = client.post("/api/v1/activate", json={"hardware_id": "HW-MISSING-KEY"})

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["valid"] is False
    assert body["code"] == "invalid_request"
    assert body["message"] == "请求参数错误"
    assert body["license_id"] is None
    assert body["status"] is None
    assert body["expire_at"] is None
    assert body["is_permanent"] is False
    assert body["remaining_days"] is None
    _assert_iso_utc_z(body["server_time"])


def test_admin_post_requires_csrf(client):
    _login_admin(client)

    response = client.post(
        "/admin/licenses/new",
        data={"customer_name": "缺少 CSRF", "duration_days": "30", "note": "should fail"},
    )

    assert response.status_code == 403


def test_production_rejects_default_secret_and_password(monkeypatch):
    monkeypatch.setenv("AUTH_ENV", "production")
    monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
    monkeypatch.delenv("AUTH_HASH_PEPPER", raising=False)
    monkeypatch.delenv("AUTH_ADMIN_PASSWORD", raising=False)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    from app.config import get_settings

    with pytest.raises(RuntimeError):
        get_settings().validate_for_startup()


def test_existing_default_admin_is_rejected_when_requested(client):
    from app.database import SessionLocal
    from app.services import seed_admin_user

    with SessionLocal() as db:
        with pytest.raises(RuntimeError, match="default password"):
            seed_admin_user(db, "admin", "replacement-password", reject_default_password=True)


def test_strict_environment_parsing(monkeypatch):
    monkeypatch.setenv("AUTH_ENV", "prod")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    with pytest.raises(RuntimeError, match="AUTH_ENV"):
        importlib.import_module("app.config").get_settings()


def test_strict_boolean_parsing(monkeypatch):
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "treu")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    with pytest.raises(RuntimeError, match="AUTH_COOKIE_SECURE"):
        importlib.import_module("app.config").get_settings()


def test_production_requires_secure_cookie(monkeypatch):
    monkeypatch.setenv("AUTH_ENV", "production")
    monkeypatch.setenv("AUTH_SECRET_KEY", "not-default-secret")
    monkeypatch.setenv("AUTH_HASH_PEPPER", "not-default-pepper")
    monkeypatch.setenv("AUTH_ADMIN_PASSWORD", "not-default-password")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    from app.config import get_settings

    with pytest.raises(RuntimeError, match="AUTH_COOKIE_SECURE"):
        get_settings().validate_for_startup()


def test_sqlite_pragmas_are_enabled(client):
    from app.database import engine

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
        assert str(connection.exec_driver_sql("PRAGMA journal_mode").scalar()).lower() == "wal"
