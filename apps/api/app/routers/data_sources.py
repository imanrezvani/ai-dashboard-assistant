import uuid

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.storage import FileNotFound, FileStorage
from app.core.storage_postgres import PostgresFileStorage
from app.middleware.tenant import get_current_organization_id, get_current_user, require_membership
from app.models.data_source import DataSource
from app.models.data_source_column import DataSourceColumn
from app.models.fact_row import FactRow
from app.models.user import User
from app.schemas.data_source import DataSourceOut, MapRequest, MapResponse, UploadPreviewResponse
from app.services.ingest import (
    MAX_UPLOAD_ROWS,
    load_dataframe,
    parse_file,
    persist_columns,
    preview_records,
)

router = APIRouter(prefix="/data-sources", tags=["data-sources"])

# storage ماندگار (فاز ۲.۱) — جایگزین PENDING_UPLOADS حافظه‌ای
# فایل به‌صورت bytea در data_source_files ذخیره می‌شود؛ پس از restart هم در دسترس است.
file_storage: FileStorage = PostgresFileStorage()

PREVIEW_ROWS = 10


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

    try:
        df = parse_file(file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if df.empty:
        raise HTTPException(status_code=400, detail="فایل هیچ ردیفی ندارد")

    # محدودیت ساده: حداکثر 10000 ردیف برای جلوگیری از overload
    if len(df) > MAX_UPLOAD_ROWS:
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
    ds_id = ds.id

    # ماندگاری فایل (bytea) + متادیتای ستون‌ها — به‌جای dict حافظه‌ای
    file_storage.save(
        db,
        data_source_id=ds_id,
        organization_id=organization_id,
        content=content,
        content_type=file.content_type,
    )
    persist_columns(db, data_source_id=ds_id, organization_id=organization_id, df=df)

    # capture مقادیر قبل از commit تا بعداً نیازی به refresh تحت RLS نباشد
    ds_name = ds.name
    ds_file_type = ds.file_type
    ds_row_count = ds.row_count
    ds_status = ds.status
    db.commit()

    # preview: 10 ردیف اول — از df که قبلاً داریم، نیازی به DB نیست
    preview = preview_records(df, PREVIEW_ROWS)

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

    # بازیابی DataFrame از storage ماندگار (bytea) — پس از restart هم کار می‌کند
    try:
        df = load_dataframe(db, ds=ds, organization_id=organization_id, storage=file_storage)
    except FileNotFound:
        raise HTTPException(status_code=400, detail="فایل برای این منبع یافت نشد؛ لطفاً دوباره آپلود کنید")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # اعتبارسنجی ستون‌ها
    cols = list(df.columns.astype(str))
    if payload.measure_column not in cols:
        raise HTTPException(status_code=400, detail=f"ستون measure '{payload.measure_column}' یافت نشد")
    for col in [payload.date_column, payload.category_column, payload.label_column]:
        if col and col not in cols:
            raise HTTPException(status_code=400, detail=f"ستون '{col}' یافت نشد")

    # ثبت نقش map شده روی متادیتای ستون‌ها (ماندگار)
    roles = {payload.measure_column: "measure"}
    for col, role in [(payload.date_column, "date"), (payload.category_column, "category"), (payload.label_column, "label")]:
        if col:
            roles[col] = role
    db.query(DataSourceColumn).filter(
        DataSourceColumn.data_source_id == ds_id,
        DataSourceColumn.organization_id == organization_id,
    ).update(
        {DataSourceColumn.mapped_role: None}, synchronize_session=False
    )
    for name, role in roles.items():
        db.query(DataSourceColumn).filter(
            DataSourceColumn.data_source_id == ds_id,
            DataSourceColumn.organization_id == organization_id,
            DataSourceColumn.name == name,
        ).update({DataSourceColumn.mapped_role: role}, synchronize_session=False)

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
