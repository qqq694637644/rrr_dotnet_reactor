from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class LicenseStatus(str, Enum):
    UNUSED = "unused"
    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class License(Base):
    __tablename__ = "licenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    license_key_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    license_key_display: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=LicenseStatus.UNUSED.value, index=True)
    hardware_id_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    hardware_id_display: Mapped[str | None] = mapped_column(String(80), nullable=True)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False, default=365)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    is_permanent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    logs: Mapped[list["LicenseLog"]] = relationship(
        back_populates="license",
        cascade="all, delete-orphan",
        order_by=lambda: LicenseLog.created_at.desc(),
    )


class LicenseLog(Base):
    __tablename__ = "license_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    license_id: Mapped[int | None] = mapped_column(ForeignKey("licenses.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    hardware_id_display: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result: Mapped[str] = mapped_column(String(30), nullable=False)
    message: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False, index=True)

    license: Mapped[License | None] = relationship(back_populates="logs")
