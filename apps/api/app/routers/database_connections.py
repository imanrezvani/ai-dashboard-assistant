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

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.connectors import ConnectorError, get_connector
from app.core.database import get_db
from app.core.credentials import encrypt_secret
from app.middleware.tenant import get_current_organization_id, get_current_user, require_role
from app.models.database_connection import DatabaseConnection
from app.models.data_source import DataSource
from app.models.user import User
from app.schemas.database_connection import (
    ConnectionTestOut,
    DatabaseConnectionCreate,
    DatabaseConnectionOut,
    ImportRequest,
    ImportResultOut,
    SampleRowsOut,
    TableOut,
)
from app.services.ingest import persist_columns, serialize_dataframe_to_csv
from app.core.storage_postgres import PostgresFileStorage

router = APIRouter(prefix="/database-connections", tags=["database-connections"])

ADMIN_ROLES = ["owner", "admin"]
MANAGER_ROLES = ["owner", "admin", "manager"]
ANALYST_ROLES = ["owner", "admin", "manager", "analyst"]

DEFAULT_SAMPLE_LIMIT = 50

# storage ماندگار — همان لایه فاز ۲.۱ که data_sources.py استفاده می‌کند
import_file_storage = PostgresFileStorage()


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


@router.post("/{conn_id}/import", response_model=ImportResultOut, status_code=201)
def import_table(
    conn_id: uuid.UUID,
    payload: ImportRequest,
    membership=Depends(require_role(ADMIN_ROLES)),
    db: Session = Depends(get_db),
):
    """Import bridge — فاز ۲.۲ گام ۴ (docs/PHASE2_PLAN.md §7).

    تنها کاری که این endpoint می‌کند: جایگزینی منبع DataFrame
    (فایل آپلودی → جدول PostgreSQL خارجی). همه‌چیز بعد از DataFrame
    دقیقاً همان مسیر فاز ۲.۱ است:
        DataSource (status=pending, file_type="postgres")
        → FileStorage.save (CSV سریال‌شده در data_source_files)
        → persist_columns (متادیتا در data_source_columns)
    و map کردن مسیر موجود POST /data-sources/{id}/map می‌ماند (fact_rows).

    امنیت:
      - اتصال فقط با RLS + فیلتر organization_id fetch می‌شود (cross-org → 404)
      - اتصال disabled → 400
      - فقط پارامترهای ساختاریافته؛ هیچ SQL خامی پذیرفته نمی‌شود
      - شناسه جدول فقط در connector با psycopg.sql.Identifier و پس از تطبیق با
        discover_tables استفاده می‌شود (تزریق → «table not found»)
      - limit در connector به MAX_FETCH_ROWS clamp می‌شود
      - هیچ رمز/DSN در پاسخ/خطا/log نیست (خطاهای connector از قبل sanitized)

    تراکنش: fetch خارجی قبل از هر INSERT اپ انجام می‌شود؛ اگر بعد از ساخت
    DataSource/فایل خطایی رخ دهد، db.rollback() همه را برمی‌گرداند — DataSource
    گمراه‌کننده‌ای با status ناقص باقی نمی‌ماند.
    """
    conn = _get_own_connection(db, conn_id=conn_id, organization_id=membership.organization_id)
    _ensure_enabled(conn)

    # ۱) واکشی از منبع خارجی — قبل از هر تغییر وضعیت محلی (مرز تراکنش تمیز)
    try:
        df: pd.DataFrame = get_connector(conn).fetch_dataframe(payload.table, payload.limit)
    except ConnectorError as exc:
        # پیام sanitized («table not found» / «connection failed» / ...) — بدون رمز/DSN
        raise HTTPException(status_code=502, detail=str(exc))

    if df.empty:
        raise HTTPException(status_code=400, detail="جدول خارجی هیچ ردیفی ندارد")

    # ۲) ساخت دقیقاً همان artifactهای upload — بدون هیچ پیاده‌سازی موازی
    name = payload.name or payload.table
    ds = DataSource(
        organization_id=membership.organization_id,
        name=name,
        file_type="postgres",
        row_count=len(df),
        status="pending",
        database_connection_id=conn.id,
    )
    db.add(ds)
    db.flush()  # id بدون commit (context RLS حفظ می‌شود)
    ds_id = ds.id

    import_file_storage.save(
        db,
        data_source_id=ds_id,
        organization_id=membership.organization_id,
        content=serialize_dataframe_to_csv(df),
        content_type="text/csv",
    )
    persist_columns(db, data_source_id=ds_id, organization_id=membership.organization_id, df=df)

    # ۳) capture مقادیر قبل از commit (context RLS تراکنش-local است)
    out_name = ds.name
    out_row_count = ds.row_count
    out_status = ds.status
    out_columns = list(df.columns.astype(str))
    conn_id_captured = conn.id
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="خطا در ثبت نتیجه import")

    return ImportResultOut(
        data_source_id=ds_id,
        name=out_name,
        file_type="postgres",
        row_count=out_row_count,
        status=out_status,
        columns=out_columns,
        database_connection_id=conn_id_captured,
    )
