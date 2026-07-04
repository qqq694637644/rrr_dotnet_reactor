from __future__ import annotations

import importlib
import sys
from datetime import timedelta
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
    assert body["license_id"] == license_id
    assert body["status"] == "active"
    assert body["remaining_days"] >= 1

    verify = client.post(
        "/api/v1/verify",
        json={"license_id": license_id, "hardware_id": "HW-001", "client_version": "1.0.0"},
    )
    assert verify.status_code == 200
    assert verify.json()["success"] is True

    heartbeat = client.post(
        "/api/v1/heartbeat",
        json={"license_key": key, "hardware_id": "HW-001", "client_version": "1.0.0"},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["success"] is True


def test_hardware_mismatch_is_rejected(client):
    _, key = _create_license(days=30)
    client.post("/api/v1/activate", json={"license_key": key, "hardware_id": "HW-A"})

    response = client.post("/api/v1/verify", json={"license_key": key, "hardware_id": "HW-B"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["message"] == "硬件不匹配"


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

    response = client.post("/api/v1/activate", json={"license_key": key, "hardware_id": "HW-NEW"})

    assert response.status_code == 200
    assert response.json()["success"] is True


def test_admin_login_and_create_license_page(client):
    login = client.post(
        "/admin/login",
        data={"username": "admin", "password": "admin123", "next": "/admin/licenses"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    response = client.post(
        "/admin/licenses/new",
        data={"customer_name": "后台客户", "duration_days": "30", "note": "后台创建"},
    )

    assert response.status_code == 200
    assert "卡密已生成" in response.text
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
