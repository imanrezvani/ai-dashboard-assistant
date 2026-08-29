import io
import uuid
from datetime import datetime

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.core.database import get_db
from app.middleware.tenant import get_current_organization_id, get_current_user, require_membership
from app.models.data_source import DataSource
from app.models.fact_row import FactRow
from app.models.user import User
from app.schemas.data_source import DataSourceOut, MapRequest, MapResponse, UploadPreviewResponse

router = APIRouter(prefix="/data-sources", tags=["data-sources"])

# حافظه موقت برای فایل‌های آپلود شده تا مرحله Map
# در محیط production باید از storage دائمی (S3/DB) استفاده کرد
PENDING_UPLOADS: dict[uuid.UUID, pd.DataFrame] = {}


def _parse_file(filename: str, content: bytes) -> pd.DataFrame:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "csv":
        try:
            return pd.read_csv(io.BytesIO(content))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"خطا در پارس CSV: {e}")
    elif ext in ("xlsx", "xls"):
        try:
            return pd.read_excel(io.BytesIO(content), engine="openpyxl")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"خطا در پارس Excel: {e}")
    else:
        raise HTTPException(status_code=400, detail="فرمت فایل باید CSV یا Excel باشد")


@router.post("/upload", response_model=UploadPreviewResponse)
def upload_data_source(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    organization_id: uuid.UUID = Depends(get_current_organization_id),
    db: Session = Depends(get_db),
):
    if not organization_id:
        raise HTTPException(status_code=400, detail="سازمان انتخاب نشده (X-Organization-Id)")
    require_membership(organization_id, db, user)

    if not file.filename:
        raise HTTPException(status_code=400, detail="نام فایل نامعتبر است")

    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="فایل خالی است")

    df = _parse_file(file.filename, content)
    if df.empty:
        raise HTTPException(status_code=400, detail="فایل هیچ ردیفی ندارد")

    # محدودیت ساده: حداکثر 10000 ردیف برای جلوگیری از overload
    if len(df) > 10000:
        raise HTTPException(status_code=400, detail="فایل بیش از ۱۰۰۰۰ ردیف دارد")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "unknown"

    ds = DataSource(
        organization_id=organization_id,
        name=file.filename,
        file_type=ext,
        row_count=len(df),
        status="pending",
    )
    db.add(ds)
    db.flush()  # تا id تولید شود، بدون commit تا context حفظ شود
    # capture مقادیر قبل از commit تا بعداً نیازی به refresh تحت RLS نباشد
    ds_id = ds.id
    ds_name = ds.name
    ds_file_type = ds.file_type
    ds_row_count = ds.row_count
    ds_status = ds.status
    # ذخیره DataFrame برای مرحله Map قبل از commit (context هنوز برقرار است)
    PENDING_UPLOADS[ds_id] = df
    db.commit()

    # preview: 10 ردیف اول — از df که قبلاً داریم، نیازی به DB نیست
    preview = df.head(10).fillna("").to_dict(orient="records")
    for row in preview:
        for k, v in list(row.items()):
            if isinstance(v, (pd.Timestamp, datetime)):
                row[k] = v.isoformat()
            elif isinstance(v, float) and pd.isna(v):
                row[k] = None

    return UploadPreviewResponse(
        id=ds_id,
        name=ds_name,
        file_type=ds_file_type,
        row_count=ds_row_count,
        status=ds_status,
        columns=list(df.columns.astype(str)),
        preview=preview,
    )


@router.post("/{ds_id}/map", response_model=MapResponse)
def map_data_source(
    ds_id: uuid.UUID,
    payload: MapRequest,
    user: User = Depends(get_current_user),
    organization_id: uuid.UUID = Depends(get_current_organization_id),
    db: Session = Depends(get_db),
):
    if not organization_id:
        raise HTTPException(status_code=400, detail="سازمان انتخاب نشده")
    require_membership(organization_id, db, user)

    ds = db.query(DataSource).filter(DataSource.id == ds_id, DataSource.organization_id == organization_id).first()
    if not ds:
        raise HTTPException(status_code=404, detail="منبع داده یافت نشد")

    # اگر DataFrame در حافظه نیست، سعی کن از DB بازیابی کنی — اما چون فایل اصلی ذخیره نشده، خطا بده
    df = PENDING_UPLOADS.get(ds_id)
    if df is None:
        raise HTTPException(status_code=400, detail="فایل برای این منبع یافت نشد؛ لطفاً دوباره آپلود کنید")

    # اعتبارسنجی ستون‌ها
    cols = list(df.columns.astype(str))
    if payload.measure_column not in cols:
        raise HTTPException(status_code=400, detail=f"ستون measure '{payload.measure_column}' یافت نشد")
    for col in [payload.date_column, payload.category_column, payload.label_column]:
        if col and col not in cols:
            raise HTTPException(status_code=400, detail=f"ستون '{col}' یافت نشد")

    # پاکسازی قبلی fact_rows برای این data_source (در صورت map مجدد)
    db.query(FactRow).filter(FactRow.data_source_id == ds_id).delete()

    inserted = 0
    for _, row in df.iterrows():
        # measure
        try:
            raw_measure = row[payload.measure_column]
            if pd.isna(raw_measure):
                continue
            measure = float(raw_measure)
        except Exception:
            continue  # ردیف‌های نامعتبر skip

        # date
        dim_date = None
        if payload.date_column:
            raw_date = row[payload.date_column]
            if not pd.isna(raw_date):
                try:
                    # pandas می‌تواند string/date را به datetime تبدیل کند
                    parsed = pd.to_datetime(raw_date, errors="coerce")
                    if not pd.isna(parsed):
                        dim_date = parsed.date()
                except Exception:
                    dim_date = None

        # category/label
        dim_cat = None
        if payload.category_column:
            v = row[payload.category_column]
            if not pd.isna(v):
                dim_cat = str(v).strip()[:255] or None
        dim_label = None
        if payload.label_column:
            v = row[payload.label_column]
            if not pd.isna(v):
                dim_label = str(v).strip()[:255] or None

        fr = FactRow(
            organization_id=organization_id,
            data_source_id=ds_id,
            measure_value=measure,
            dimension_date=dim_date,
            dimension_category=dim_cat,
            dimension_label=dim_label,
        )
        db.add(fr)
        inserted += 1

    ds.status = "mapped"
    ds.row_count = inserted
    # capture قبل از commit
    final_status = ds.status
    db.commit()

    # پاکسازی حافظه موقت
    PENDING_UPLOADS.pop(ds_id, None)

    return MapResponse(data_source_id=ds_id, inserted_rows=inserted, status=final_status)


@router.get("", response_model=list[DataSourceOut])
def list_data_sources(
    user: User = Depends(get_current_user),
    organization_id: uuid.UUID = Depends(get_current_organization_id),
    db: Session = Depends(get_db),
):
    if not organization_id:
        raise HTTPException(status_code=400, detail="سازمان انتخاب نشده")
    require_membership(organization_id, db, user)
    return db.query(DataSource).filter(DataSource.organization_id == organization_id).order_by(DataSource.uploaded_at.desc()).all()


@router.get("/{ds_id}", response_model=DataSourceOut)
def get_data_source(
    ds_id: uuid.UUID,
    user: User = Depends(get_current_user),
    organization_id: uuid.UUID = Depends(get_current_organization_id),
    db: Session = Depends(get_db),
):
    if not organization_id:
        raise HTTPException(status_code=400, detail="سازمان انتخاب نشده")
    require_membership(organization_id, db, user)
    ds = db.query(DataSource).filter(DataSource.id == ds_id, DataSource.organization_id == organization_id).first()
    if not ds:
        raise HTTPException(status_code=404, detail="منبع یافت نشد")
    return ds


@router.get("/{ds_id}/rows")
def list_fact_rows(
    ds_id: uuid.UUID,
    user: User = Depends(get_current_user),
    organization_id: uuid.UUID = Depends(get_current_organization_id),
    db: Session = Depends(get_db),
):
    if not organization_id:
        raise HTTPException(status_code=400, detail="سازمان انتخاب نشده")
    require_membership(organization_id, db, user)
    # تأیید تعلق data_source
    ds = db.query(DataSource).filter(DataSource.id == ds_id, DataSource.organization_id == organization_id).first()
    if not ds:
        raise HTTPException(status_code=404, detail="منبع یافت نشد")
    rows = db.query(FactRow).filter(FactRow.data_source_id == ds_id).order_by(FactRow.created_at).limit(100).all()
    return [
        {
            "id": str(r.id),
            "measure_value": float(r.measure_value),
            "dimension_date": r.dimension_date.isoformat() if r.dimension_date else None,
            "dimension_category": r.dimension_category,
            "dimension_label": r.dimension_label,
        }
        for r in rows
    ]
