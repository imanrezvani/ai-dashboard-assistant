"""راستر API اتصالات دیتابیس خارجی — فاز ۲.۲ گام ۳ (docs/PHASE2_PLAN.md §6)

مرزهای امنیتی این راستر (طبق طرح):
  - ماتریس نقش‌ها (کمترین حد ممکن):
      create/delete/test → admin+   (مدیریت اعتبارنامه / اتصال خروجی)
      list/get/tables     → manager+
      sample              → analyst+
  - cross-org id → 404 (نه 403) تا وجود ردیف لو نرود — با RLS + فیلتر صریح organization_id
  - `password` write-only: فقط encrypt و ذخیره؛ هیچ پاسخی رمز/hint/DSN برنمی‌گرداند
  - connector فقط پس از fetch سازمان‌محورِ ردیف ساخته می‌شود → decrypt تنها نقطه ممکن
  - اتصال disabled (enabled=False) → 400 روی test/tables/sample
  - هیچ PUT/PATCH وجود ندارد (rotate = delete + recreate)
  - خطاهای connector از قبل sanitized هستند؛ here فقط HTTPException با همان متن امن
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.connectors import ConnectorError, get_connector
from app.core.database import get_db
from app.core.credentials import encrypt_secret
from app.middleware.tenant import get_current_organization_id, get_current_user, require_role
from app.models.database_connection import DatabaseConnection
from app.models.user import User
from app.schemas.database_connection import (
    ConnectionTestOut,
    DatabaseConnectionCreate,
    DatabaseConnectionOut,
    SampleRowsOut,
    TableOut,
)

router = APIRouter(prefix="/database-connections", tags=["database-connections"])

ADMIN_ROLES = ["owner", "admin"]
MANAGER_ROLES = ["owner", "admin", "manager"]
ANALYST_ROLES = ["owner", "admin", "manager", "analyst"]

DEFAULT_SAMPLE_LIMIT = 50


def _get_own_connection(db: Session, *, conn_id: uuid.UUID, organization_id: uuid.UUID) -> DatabaseConnection:
    """fetch سازمان‌محور — RLS + فیلتر صریح. cross-org → 404 (بدون افشای وجود)."""
    conn = (
        db.query(DatabaseConnection)
        .filter(
            DatabaseConnection.id == conn_id,
            DatabaseConnection.organization_id == organization_id,
        )
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="اتصال یافت نشد")
    return conn


def _ensure_enabled(conn: DatabaseConnection) -> None:
    if not conn.enabled:
        raise HTTPException(status_code=400, detail="این اتصال غیرفعال است")


@router.post("", response_model=DatabaseConnectionOut, status_code=201)
def create_connection(
    payload: DatabaseConnectionCreate,
    membership=Depends(require_role(ADMIN_ROLES)),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # membership.organization_id همان tenant تأییدشده است (RBAC + RLS)
    organization_id = membership.organization_id

    conn = DatabaseConnection(
        organization_id=organization_id,
        name=payload.name,
        engine=payload.engine,
        host=payload.host,
        port=payload.port,
        database_name=payload.database_name,
        username=payload.username,
        # فقط ciphertext ذخیره می‌شود — plaintext پس از این نقطه هرگز استفاده نمی‌شود
        encrypted_password=encrypt_secret(payload.password),
        ssl_mode=payload.ssl_mode,
    )
    db.add(conn)
    try:
        db.flush()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409, detail="نام اتصال در این سازمان تکراری است")
    # capture قبل از commit (context RLS تراکنش-local است — الگوی data_sources)
    out = {
        "id": conn.id,
        "organization_id": conn.organization_id,
        "name": conn.name,
        "engine": conn.engine,
        "host": conn.host,
        "port": conn.port,
        "database_name": conn.database_name,
        "username": conn.username,
        "ssl_mode": conn.ssl_mode,
        "enabled": conn.enabled,
        "status": conn.status,
        "last_checked_at": conn.last_checked_at,
        "last_error": conn.last_error,
        "has_stored_credentials": True,
        "created_at": conn.created_at,
        "updated_at": conn.updated_at,
    }
    db.commit()
    return out


@router.get("", response_model=list[DatabaseConnectionOut])
def list_connections(
    membership=Depends(require_role(MANAGER_ROLES)),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(DatabaseConnection)
        .filter(DatabaseConnection.organization_id == membership.organization_id)
        .order_by(DatabaseConnection.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "organization_id": r.organization_id,
            "name": r.name,
            "engine": r.engine,
            "host": r.host,
            "port": r.port,
            "database_name": r.database_name,
            "username": r.username,
            "ssl_mode": r.ssl_mode,
            "enabled": r.enabled,
            "status": r.status,
            "last_checked_at": r.last_checked_at,
            "last_error": r.last_error,
            "has_stored_credentials": True,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


@router.get("/{conn_id}", response_model=DatabaseConnectionOut)
def get_connection(
    conn_id: uuid.UUID,
    membership=Depends(require_role(MANAGER_ROLES)),
    db: Session = Depends(get_db),
):
    conn = _get_own_connection(db, conn_id=conn_id, organization_id=membership.organization_id)
    return {
        "id": conn.id,
        "organization_id": conn.organization_id,
        "name": conn.name,
        "engine": conn.engine,
        "host": conn.host,
        "port": conn.port,
        "database_name": conn.database_name,
        "username": conn.username,
        "ssl_mode": conn.ssl_mode,
        "enabled": conn.enabled,
        "status": conn.status,
        "last_checked_at": conn.last_checked_at,
        "last_error": conn.last_error,
        "has_stored_credentials": True,
        "created_at": conn.created_at,
        "updated_at": conn.updated_at,
    }


@router.delete("/{conn_id}", status_code=204)
def delete_connection(
    conn_id: uuid.UUID,
    membership=Depends(require_role(ADMIN_ROLES)),
    db: Session = Depends(get_db),
):
    conn = _get_own_connection(db, conn_id=conn_id, organization_id=membership.organization_id)
    db.delete(conn)
    db.commit()
    return None


@router.post("/{conn_id}/test", response_model=ConnectionTestOut)
def test_connection(
    conn_id: uuid.UUID,
    membership=Depends(require_role(ADMIN_ROLES)),
    db: Session = Depends(get_db),
):
    """اجرای test_connection با اعتبارنامه ذخیره‌شده (ادمین+) —
    وضعیت/زمان/خطای sanitized روی ردیف ماندگار می‌شود."""
    conn = _get_own_connection(db, conn_id=conn_id, organization_id=membership.organization_id)
    _ensure_enabled(conn)

    check = get_connector(conn).test_connection()
    conn.status = "ok" if check.ok else "failed"
    conn.last_checked_at = datetime.now(timezone.utc)
    conn.last_error = check.error
    try:
        db.flush()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="خطا در ثبت وضعیت اتصال")
    out = {
        "id": conn.id,
        "status": conn.status,
        "ok": check.ok,
        "latency_ms": check.latency_ms,
        "last_checked_at": conn.last_checked_at,
        "last_error": conn.last_error,
    }
    db.commit()
    return out


@router.get("/{conn_id}/tables", response_model=list[TableOut])
def list_tables(
    conn_id: uuid.UUID,
    membership=Depends(require_role(MANAGER_ROLES)),
    db: Session = Depends(get_db),
):
    conn = _get_own_connection(db, conn_id=conn_id, organization_id=membership.organization_id)
    _ensure_enabled(conn)
    try:
        tables = get_connector(conn).discover_tables()
    except ConnectorError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return [{"schema_name": t.schema, "name": t.name, "qualified_name": t.qualified_name} for t in tables]


@router.get("/{conn_id}/tables/{table}/sample", response_model=SampleRowsOut)
def sample_table(
    conn_id: uuid.UUID,
    table: str,
    limit: int = Query(default=DEFAULT_SAMPLE_LIMIT, ge=1, le=1000),
    membership=Depends(require_role(ANALYST_ROLES)),
    db: Session = Depends(get_db),
):
    conn = _get_own_connection(db, conn_id=conn_id, organization_id=membership.organization_id)
    _ensure_enabled(conn)
    try:
        rows = get_connector(conn).sample_rows(table, limit)
    except ConnectorError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"table": table, "limit": limit, "rows": rows}
