from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import SessionLocal, get_db, init_db
from .models import AdminUser, License, LicenseLog, LicenseStatus
from .schemas import ActivateRequest, CheckRequest, LicenseCheckResponse
from .security import (
    clear_session_cookie,
    create_csrf_token,
    get_client_ip,
    iso_utc_z,
    read_session_token,
    set_session_cookie,
    utcnow,
    verify_csrf_token,
    verify_password,
)
from .services import (
    activate_license,
    check_license,
    create_license,
    delete_license,
    disable_license,
    remaining_days,
    renew_license,
    restore_license,
    seed_admin_user,
    set_custom_expire_at,
    set_permanent,
    unbind_license,
    update_expired_status,
)

BASE_DIR = Path(__file__).resolve().parent
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate_for_startup()
    init_db()
    with SessionLocal() as db:
        seed_admin_user(
            db,
            settings.admin_username,
            settings.admin_password,
            reject_default_password=settings.environment == "production",
        )
    yield


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def render_template(request: Request, name: str, context: dict[str, object] | None = None, status_code: int = 200) -> Response:
    template_context: dict[str, object] = {"request": request}
    if context:
        template_context.update(context)
    template_context.setdefault("csrf_token", create_csrf_token(request.cookies.get(settings.session_cookie_name)))
    return templates.TemplateResponse(request=request, name=name, context=template_context, status_code=status_code)


class LoginRequired(Exception):
    pass


def fmt_datetime(value: datetime | None) -> str:
    if not value:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def status_label(status: str) -> str:
    labels = {
        LicenseStatus.UNUSED.value: "未激活",
        LicenseStatus.ACTIVE.value: "正常",
        LicenseStatus.EXPIRED.value: "已过期",
        LicenseStatus.DISABLED.value: "已禁用",
        LicenseStatus.DELETED.value: "已删除",
    }
    return labels.get(status, status)


def remaining_label(license: License) -> str:
    if license.is_permanent:
        return "永久"
    days = remaining_days(license)
    if days is None:
        return "-"
    return f"{days} 天"


templates.env.filters["dt"] = fmt_datetime
templates.env.filters["status_label"] = status_label
templates.env.filters["remaining"] = remaining_label


def api_response(result) -> LicenseCheckResponse:
    license = result.license
    return LicenseCheckResponse(
        success=result.success,
        valid=result.success,
        code=result.code,
        message=result.message,
        license_id=license.id if license else None,
        status=license.status if license else None,
        expire_at=iso_utc_z(license.expire_at) if license else None,
        is_permanent=bool(license.is_permanent) if license else False,
        remaining_days=remaining_days(license) if license else None,
        server_time=iso_utc_z(utcnow()),
    )


def protocol_error(code: str, message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "valid": False,
            "code": code,
            "message": message,
            "license_id": None,
            "status": None,
            "expire_at": None,
            "is_permanent": False,
            "remaining_days": None,
            "server_time": iso_utc_z(utcnow()),
        },
    )


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def bad_request_from_value_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def safe_next_path(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/admin/licenses"


def get_current_admin(request: Request, db: Session) -> AdminUser | None:
    token = request.cookies.get(settings.session_cookie_name)
    data = read_session_token(token)
    if not data:
        return None
    admin_id = data.get("admin_id")
    if not isinstance(admin_id, int):
        return None
    return db.get(AdminUser, admin_id)


def require_admin(request: Request, db: Annotated[Session, Depends(get_db)]) -> AdminUser:
    admin = get_current_admin(request, db)
    if not admin:
        raise LoginRequired()
    return admin


def require_csrf(request: Request, csrf_token: Annotated[str | None, Form()] = None) -> None:
    session_token = request.cookies.get(settings.session_cookie_name)
    if not verify_csrf_token(session_token, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token 无效")


@app.exception_handler(LoginRequired)
def login_required_handler(request: Request, exc: LoginRequired) -> RedirectResponse:
    return redirect(f"/admin/login?next={request.url.path}")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "server_time": iso_utc_z(utcnow())}


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return redirect("/admin/licenses")


@app.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, next: str = "/admin/licenses") -> Response:
    return render_template(request, "login.html", {"error": None, "next": safe_next_path(next)})


@app.post("/admin/login", response_class=HTMLResponse, include_in_schema=False)
def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/admin/licenses",
    db: Session = Depends(get_db),
) -> Response:
    admin = db.scalar(select(AdminUser).where(AdminUser.username == username.strip()))
    if not admin or not verify_password(password, admin.password_hash):
        return render_template(request, "login.html", {"error": "用户名或密码错误", "next": safe_next_path(next)}, status_code=401)

    admin.last_login_at = utcnow()
    db.commit()
    response = redirect(safe_next_path(next))
    set_session_cookie(response, admin.id)
    return response


@app.post("/admin/logout", include_in_schema=False)
def logout(csrf: Annotated[None, Depends(require_csrf)]) -> Response:
    response = redirect("/admin/login")
    clear_session_cookie(response)
    return response


@app.get("/admin/licenses", response_class=HTMLResponse, include_in_schema=False)
def license_list(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Session = Depends(get_db),
    status: str | None = None,
) -> Response:
    query = select(License).order_by(desc(License.created_at), desc(License.id))
    if status:
        query = query.where(License.status == status)
    licenses = list(db.scalars(query))
    changed = False
    for license in licenses:
        before = license.status
        update_expired_status(license)
        changed = changed or before != license.status
    if changed:
        db.commit()
    return render_template(request, "licenses.html", {"admin": admin, "licenses": licenses, "status": status, "statuses": list(LicenseStatus)})


@app.get("/admin/logs", response_class=HTMLResponse, include_in_schema=False)
def log_list(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Session = Depends(get_db),
    event_type: str | None = None,
    result: str | None = None,
    limit: int = 200,
) -> Response:
    limit = min(max(limit, 1), 500)
    query = select(LicenseLog).order_by(desc(LicenseLog.created_at), desc(LicenseLog.id)).limit(limit)
    if event_type:
        query = query.where(LicenseLog.event_type == event_type)
    if result:
        query = query.where(LicenseLog.result == result)
    logs = list(db.scalars(query))
    return render_template(
        request,
        "logs.html",
        {
            "admin": admin,
            "logs": logs,
            "event_type": event_type,
            "result": result,
            "event_types": ["create", "activate", "verify", "heartbeat", "renew", "disable", "unbind", "delete"],
            "results": ["success", "failed"],
            "limit": limit,
        },
    )


@app.get("/admin/licenses/new", response_class=HTMLResponse, include_in_schema=False)
def new_license_page(request: Request, admin: Annotated[AdminUser, Depends(require_admin)]) -> Response:
    return render_template(request, "new_license.html", {"admin": admin, "generated_key": None, "license": None, "error": None})


@app.post("/admin/licenses/new", response_class=HTMLResponse, include_in_schema=False)
def new_license(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_admin)],
    csrf: Annotated[None, Depends(require_csrf)],
    customer_name: Annotated[str, Form()],
    duration_days: Annotated[int, Form()] = 365,
    is_permanent: Annotated[bool, Form()] = False,
    note: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> Response:
    try:
        license, raw_key = create_license(
            db,
            customer_name=customer_name,
            duration_days=duration_days,
            is_permanent=is_permanent,
            note=note,
            ip=get_client_ip(request),
        )
    except ValueError as exc:
        return render_template(request, "new_license.html", {"admin": admin, "generated_key": None, "license": None, "error": str(exc)}, status_code=400)
    return render_template(request, "new_license.html", {"admin": admin, "generated_key": raw_key, "license": license, "error": None})


@app.get("/admin/licenses/{license_id}", response_class=HTMLResponse, include_in_schema=False)
def license_detail(
    request: Request,
    license_id: int,
    admin: Annotated[AdminUser, Depends(require_admin)],
    db: Session = Depends(get_db),
) -> Response:
    license = db.get(License, license_id)
    if not license:
        raise HTTPException(status_code=404, detail="授权不存在")
    update_expired_status(license)
    db.commit()
    logs = list(license.logs[:50])
    return render_template(request, "license_detail.html", {"admin": admin, "license": license, "logs": logs, "error": None})


@app.post("/admin/licenses/{license_id}/renew", include_in_schema=False)
def renew_license_route(
    request: Request,
    license_id: int,
    admin: Annotated[AdminUser, Depends(require_admin)],
    csrf: Annotated[None, Depends(require_csrf)],
    days: Annotated[int | None, Form()] = None,
    custom_expire_at: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> Response:
    license = db.get(License, license_id)
    if not license:
        raise HTTPException(status_code=404, detail="授权不存在")
    if custom_expire_at.strip():
        raw = custom_expire_at.strip()
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="自定义到期时间格式错误") from exc
        try:
            set_custom_expire_at(db, license, parsed, ip=get_client_ip(request))
        except ValueError as exc:
            raise bad_request_from_value_error(exc) from exc
    elif days:
        try:
            renew_license(db, license, days, ip=get_client_ip(request))
        except ValueError as exc:
            raise bad_request_from_value_error(exc) from exc
    else:
        raise HTTPException(status_code=400, detail="请选择续期天数或填写自定义到期时间")
    return redirect(f"/admin/licenses/{license_id}")


@app.post("/admin/licenses/{license_id}/permanent", include_in_schema=False)
def permanent_license_route(
    request: Request,
    license_id: int,
    admin: Annotated[AdminUser, Depends(require_admin)],
    csrf: Annotated[None, Depends(require_csrf)],
    value: Annotated[bool, Form()],
    db: Session = Depends(get_db),
) -> Response:
    license = db.get(License, license_id)
    if not license:
        raise HTTPException(status_code=404, detail="授权不存在")
    try:
        set_permanent(db, license, value, ip=get_client_ip(request))
    except ValueError as exc:
        raise bad_request_from_value_error(exc) from exc
    return redirect(f"/admin/licenses/{license_id}")


@app.post("/admin/licenses/{license_id}/disable", include_in_schema=False)
def disable_license_route(
    request: Request,
    license_id: int,
    admin: Annotated[AdminUser, Depends(require_admin)],
    csrf: Annotated[None, Depends(require_csrf)],
    db: Session = Depends(get_db),
) -> Response:
    license = db.get(License, license_id)
    if not license:
        raise HTTPException(status_code=404, detail="授权不存在")
    try:
        disable_license(db, license, ip=get_client_ip(request))
    except ValueError as exc:
        raise bad_request_from_value_error(exc) from exc
    return redirect(f"/admin/licenses/{license_id}")


@app.post("/admin/licenses/{license_id}/restore", include_in_schema=False)
def restore_license_route(
    request: Request,
    license_id: int,
    admin: Annotated[AdminUser, Depends(require_admin)],
    csrf: Annotated[None, Depends(require_csrf)],
    db: Session = Depends(get_db),
) -> Response:
    license = db.get(License, license_id)
    if not license:
        raise HTTPException(status_code=404, detail="授权不存在")
    try:
        restore_license(db, license, ip=get_client_ip(request))
    except ValueError as exc:
        raise bad_request_from_value_error(exc) from exc
    return redirect(f"/admin/licenses/{license_id}")


@app.post("/admin/licenses/{license_id}/unbind", include_in_schema=False)
def unbind_license_route(
    request: Request,
    license_id: int,
    admin: Annotated[AdminUser, Depends(require_admin)],
    csrf: Annotated[None, Depends(require_csrf)],
    db: Session = Depends(get_db),
) -> Response:
    license = db.get(License, license_id)
    if not license:
        raise HTTPException(status_code=404, detail="授权不存在")
    try:
        unbind_license(db, license, ip=get_client_ip(request))
    except ValueError as exc:
        raise bad_request_from_value_error(exc) from exc
    return redirect(f"/admin/licenses/{license_id}")


@app.post("/admin/licenses/{license_id}/delete", include_in_schema=False)
def delete_license_route(
    request: Request,
    license_id: int,
    admin: Annotated[AdminUser, Depends(require_admin)],
    csrf: Annotated[None, Depends(require_csrf)],
    db: Session = Depends(get_db),
) -> Response:
    license = db.get(License, license_id)
    if not license:
        raise HTTPException(status_code=404, detail="授权不存在")
    try:
        delete_license(db, license, ip=get_client_ip(request))
    except ValueError as exc:
        raise bad_request_from_value_error(exc) from exc
    return redirect("/admin/licenses")


@app.post("/api/v1/activate", response_model=LicenseCheckResponse)
def api_activate(payload: ActivateRequest, request: Request, db: Session = Depends(get_db)) -> LicenseCheckResponse:
    result = activate_license(
        db,
        license_key=payload.license_key,
        hardware_id=payload.hardware_id,
        client_version=payload.client_version,
        ip=get_client_ip(request),
    )
    return api_response(result)


@app.post("/api/v1/verify", response_model=LicenseCheckResponse)
def api_verify(payload: CheckRequest, request: Request, db: Session = Depends(get_db)) -> Response:
    if not payload.license_key and payload.license_id is None:
        return protocol_error("missing_license_identifier", "license_key 或 license_id 必须提供一个")
    result = check_license(
        db,
        event_type="verify",
        license_key=payload.license_key,
        license_id=payload.license_id,
        hardware_id=payload.hardware_id,
        client_version=payload.client_version,
        ip=get_client_ip(request),
    )
    return api_response(result)


@app.post("/api/v1/heartbeat", response_model=LicenseCheckResponse)
def api_heartbeat(payload: CheckRequest, request: Request, db: Session = Depends(get_db)) -> Response:
    if not payload.license_key and payload.license_id is None:
        return protocol_error("missing_license_identifier", "license_key 或 license_id 必须提供一个")
    result = check_license(
        db,
        event_type="heartbeat",
        license_key=payload.license_key,
        license_id=payload.license_id,
        hardware_id=payload.hardware_id,
        client_version=payload.client_version,
        ip=get_client_ip(request),
    )
    return api_response(result)
