from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ActivateRequest(BaseModel):
    license_key: str = Field(..., min_length=4)
    hardware_id: str = Field(..., min_length=1)
    client_version: str | None = None


class CheckRequest(BaseModel):
    license_key: str | None = None
    license_id: int | None = None
    hardware_id: str = Field(..., min_length=1)
    client_version: str | None = None


class LicenseCheckResponse(BaseModel):
    success: bool
    valid: bool
    message: str
    license_id: int | None
    status: str | None
    expire_at: datetime | None
    is_permanent: bool
    remaining_days: int | None
    server_time: datetime
