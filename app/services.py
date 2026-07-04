from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import AdminUser, License, LicenseLog, LicenseStatus
from .security import (
    display_hardware_id,
    display_license_key,
    hash_hardware_id,
    hash_license_key,
    hash_password,
    make_license_key,
    normalize_hardware_id,
    utcnow,
)


@dataclass(frozen=True)
class LicenseResult:
    success: bool
    message: str
    license: License | None = None


def seed_admin_user(db: Session, username: str, password: str) -> AdminUser:
    existing = db.scalar(select(AdminUser).where(AdminUser.username == username))
    if existing:
        return existing

    admin = AdminUser(username=username, password_hash=hash_password(password))
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def add_log(
    db: Session,
    *,
    license: License | None,
    event_type: str,
    hardware_id: str | None,
    ip: str | None,
    client_version: str | None,
    result: str,
    message: str,
) -> LicenseLog:
    log = LicenseLog(
        license_id=license.id if license else None,
        event_type=event_type,
        hardware_id_display=display_hardware_id(hardware_id) if hardware_id else None,
        ip=ip,
        client_version=client_version,
        result=result,
        message=message,
    )
    db.add(log)
    return log


def create_license(
    db: Session,
    *,
    customer_name: str,
    duration_days: int,
    is_permanent: bool,
    note: str,
    ip: str | None = None,
) -> tuple[License, str]:
    if duration_days <= 0 and not is_permanent:
        raise ValueError("授权天数必须大于 0")

    for _ in range(10):
        raw_key = make_license_key()
        key_hash = hash_license_key(raw_key)
        exists = db.scalar(select(License.id).where(License.license_key_hash == key_hash))
        if not exists:
            break
    else:
        raise RuntimeError("无法生成唯一卡密")

    license = License(
        license_key_hash=key_hash,
        license_key_display=display_license_key(raw_key),
        customer_name=customer_name.strip(),
        status=LicenseStatus.UNUSED.value,
        duration_days=duration_days,
        is_permanent=is_permanent,
        note=note.strip(),
    )
    db.add(license)
    db.flush()
    add_log(
        db,
        license=license,
        event_type="create",
        hardware_id=None,
        ip=ip,
        client_version=None,
        result="success",
        message="创建卡密",
    )
    db.commit()
    db.refresh(license)
    return license, raw_key


def find_license(db: Session, *, license_key: str | None = None, license_id: int | None = None) -> License | None:
    if license_id is not None:
        return db.get(License, license_id)
    if license_key:
        return db.scalar(select(License).where(License.license_key_hash == hash_license_key(license_key)))
    return None


def remaining_days(license: License, now: datetime | None = None) -> int | None:
    if license.is_permanent:
        return None
    if not license.expire_at:
        return None
    now = now or utcnow()
    seconds = (license.expire_at - now).total_seconds()
    if seconds <= 0:
        return 0
    return max(1, int((seconds + 86399) // 86400))


def update_expired_status(license: License, now: datetime | None = None) -> None:
    now = now or utcnow()
    if (
        license.status == LicenseStatus.ACTIVE.value
        and not license.is_permanent
        and license.expire_at is not None
        and license.expire_at <= now
    ):
        license.status = LicenseStatus.EXPIRED.value


def _success_result(license: License, message: str) -> LicenseResult:
    return LicenseResult(success=True, message=message, license=license)


def _fail_result(db: Session, license: License | None, event_type: str, hardware_id: str, ip: str, client_version: str | None, message: str) -> LicenseResult:
    add_log(
        db,
        license=license,
        event_type=event_type,
        hardware_id=hardware_id,
        ip=ip,
        client_version=client_version,
        result="failed",
        message=message,
    )
    if license:
        license.last_check_at = utcnow()
        license.last_ip = ip
    db.commit()
    return LicenseResult(success=False, message=message, license=license)


def activate_license(db: Session, *, license_key: str, hardware_id: str, client_version: str | None, ip: str) -> LicenseResult:
    now = utcnow()
    normalized_hardware = normalize_hardware_id(hardware_id)
    if not normalized_hardware:
        return LicenseResult(success=False, message="Hardware ID 不能为空")

    license = find_license(db, license_key=license_key)
    if not license:
        add_log(
            db,
            license=None,
            event_type="activate",
            hardware_id=hardware_id,
            ip=ip,
            client_version=client_version,
            result="failed",
            message="卡密不存在",
        )
        db.commit()
        return LicenseResult(success=False, message="卡密不存在")

    update_expired_status(license, now)
    if license.status == LicenseStatus.DISABLED.value:
        return _fail_result(db, license, "activate", hardware_id, ip, client_version, "卡密已禁用")
    if license.status == LicenseStatus.EXPIRED.value:
        return _fail_result(db, license, "activate", hardware_id, ip, client_version, "授权已过期")
    if license.hardware_id_hash:
        return _fail_result(db, license, "activate", hardware_id, ip, client_version, "卡密已被使用")
    if license.status not in {LicenseStatus.UNUSED.value, LicenseStatus.ACTIVE.value}:
        return _fail_result(db, license, "activate", hardware_id, ip, client_version, "卡密已被使用")

    expire_at = None if license.is_permanent else license.expire_at or now + timedelta(days=license.duration_days)
    stmt = (
        update(License)
        .where(
            License.id == license.id,
            License.hardware_id_hash.is_(None),
            License.status.in_([LicenseStatus.UNUSED.value, LicenseStatus.ACTIVE.value]),
        )
        .values(
            hardware_id_hash=hash_hardware_id(normalized_hardware),
            hardware_id_display=display_hardware_id(normalized_hardware),
            activated_at=license.activated_at or now,
            expire_at=expire_at,
            status=LicenseStatus.ACTIVE.value,
            last_check_at=now,
            last_ip=ip,
        )
    )
    result = db.execute(stmt)
    if result.rowcount != 1:
        db.rollback()
        db.refresh(license)
        return _fail_result(db, license, "activate", hardware_id, ip, client_version, "卡密已被使用")

    db.refresh(license)
    add_log(
        db,
        license=license,
        event_type="activate",
        hardware_id=hardware_id,
        ip=ip,
        client_version=client_version,
        result="success",
        message="激活成功",
    )
    db.commit()
    db.refresh(license)
    return _success_result(license, "激活成功")


def check_license(
    db: Session,
    *,
    event_type: str,
    license_key: str | None,
    license_id: int | None,
    hardware_id: str,
    client_version: str | None,
    ip: str,
) -> LicenseResult:
    now = utcnow()
    normalized_hardware = normalize_hardware_id(hardware_id)
    if not normalized_hardware:
        return LicenseResult(success=False, message="Hardware ID 不能为空")

    license = find_license(db, license_key=license_key, license_id=license_id)
    if not license:
        add_log(
            db,
            license=None,
            event_type=event_type,
            hardware_id=hardware_id,
            ip=ip,
            client_version=client_version,
            result="failed",
            message="授权不存在",
        )
        db.commit()
        return LicenseResult(success=False, message="授权不存在")

    update_expired_status(license, now)
    if license.status == LicenseStatus.UNUSED.value or not license.activated_at:
        return _fail_result(db, license, event_type, hardware_id, ip, client_version, "授权未激活")
    if license.status == LicenseStatus.DISABLED.value:
        return _fail_result(db, license, event_type, hardware_id, ip, client_version, "授权已禁用")
    if license.hardware_id_hash != hash_hardware_id(normalized_hardware):
        return _fail_result(db, license, event_type, hardware_id, ip, client_version, "硬件不匹配")
    if license.status == LicenseStatus.EXPIRED.value:
        return _fail_result(db, license, event_type, hardware_id, ip, client_version, "授权已过期")

    license.status = LicenseStatus.ACTIVE.value
    license.last_check_at = now
    license.last_ip = ip
    add_log(
        db,
        license=license,
        event_type=event_type,
        hardware_id=hardware_id,
        ip=ip,
        client_version=client_version,
        result="success",
        message="授权有效",
    )
    db.commit()
    db.refresh(license)
    return _success_result(license, "授权有效")


def renew_license(db: Session, license: License, days: int, *, ip: str | None = None) -> License:
    if days <= 0:
        raise ValueError("续期天数必须大于 0")
    now = utcnow()
    base_time = license.expire_at if license.expire_at and license.expire_at > now else now
    license.expire_at = base_time + timedelta(days=days)
    license.is_permanent = False
    license.duration_days = max(license.duration_days, days)
    if license.status == LicenseStatus.EXPIRED.value and license.activated_at:
        license.status = LicenseStatus.ACTIVE.value
    add_log(
        db,
        license=license,
        event_type="renew",
        hardware_id=None,
        ip=ip,
        client_version=None,
        result="success",
        message=f"续期 {days} 天",
    )
    db.commit()
    db.refresh(license)
    return license


def set_custom_expire_at(db: Session, license: License, expire_at: datetime, *, ip: str | None = None) -> License:
    license.expire_at = expire_at
    license.is_permanent = False
    if license.activated_at:
        license.status = LicenseStatus.ACTIVE.value if expire_at > utcnow() else LicenseStatus.EXPIRED.value
    add_log(
        db,
        license=license,
        event_type="renew",
        hardware_id=None,
        ip=ip,
        client_version=None,
        result="success",
        message=f"设置到期时间 {expire_at.isoformat(sep=' ', timespec='minutes')}",
    )
    db.commit()
    db.refresh(license)
    return license


def set_permanent(db: Session, license: License, value: bool, *, ip: str | None = None) -> License:
    license.is_permanent = value
    if value:
        license.expire_at = None
        if license.activated_at and license.status == LicenseStatus.EXPIRED.value:
            license.status = LicenseStatus.ACTIVE.value
    elif license.activated_at and license.expire_at is None:
        license.expire_at = utcnow() + timedelta(days=license.duration_days)
        if license.status == LicenseStatus.EXPIRED.value:
            license.status = LicenseStatus.ACTIVE.value
    add_log(
        db,
        license=license,
        event_type="renew",
        hardware_id=None,
        ip=ip,
        client_version=None,
        result="success",
        message="设置永久授权" if value else "取消永久授权",
    )
    db.commit()
    db.refresh(license)
    return license


def disable_license(db: Session, license: License, *, ip: str | None = None) -> License:
    license.status = LicenseStatus.DISABLED.value
    add_log(db, license=license, event_type="disable", hardware_id=None, ip=ip, client_version=None, result="success", message="禁用授权")
    db.commit()
    db.refresh(license)
    return license


def restore_license(db: Session, license: License, *, ip: str | None = None) -> License:
    if not license.activated_at:
        license.status = LicenseStatus.UNUSED.value
    elif not license.is_permanent and license.expire_at and license.expire_at <= utcnow():
        license.status = LicenseStatus.EXPIRED.value
    else:
        license.status = LicenseStatus.ACTIVE.value
    add_log(db, license=license, event_type="renew", hardware_id=None, ip=ip, client_version=None, result="success", message="恢复授权")
    db.commit()
    db.refresh(license)
    return license


def unbind_license(db: Session, license: License, *, ip: str | None = None) -> License:
    license.hardware_id_hash = None
    license.hardware_id_display = None
    add_log(db, license=license, event_type="unbind", hardware_id=None, ip=ip, client_version=None, result="success", message="解绑机器")
    db.commit()
    db.refresh(license)
    return license
