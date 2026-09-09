"""لایه مشترک ingestion — استخراج بدون تغییر رفتار از app/routers/data_sources.py

طبق docs/PHASE2_PLAN.md §7: مسیر نرمال‌سازی فاز ۲.۱
(آپلود → data_source_files (bytea) + data_source_columns → map → fact_rows)
برای منابع خارجی هم استفاده می‌شود؛ تنها «منبع DataFrame» عوض می‌شود
(فایل آپلودی → جدول PostgreSQL خارجی از طریق connector). همه‌چیز بعد از
DataFrame یکسان می‌ماند — هیچ پیاده‌سازی موازی ingestion وجود ندارد.

توابع این ماژول توسط data_sources.py (آپلود CSV/Excel) و
database_connections.py (import) استفاده می‌شوند.
"""

import io
from datetime import datetime

import pandas as pd
from sqlalchemy.orm import Session

from app.core.storage import FileStorage
from app.models.data_source import DataSource
from app.models.data_source_column import DataSourceColumn

# سقف ردیف آپلود — همان مقدار قبلی data_sources.py (رفتار تغییر نکرده)
MAX_UPLOAD_ROWS = 10000


def infer_dtype(dtype) -> str | None:
    """نوع ستون pandas به رشته کوتاه قابل ذخیره (قبلاً _infer_dtype در data_sources.py)."""
    if dtype is None:
        return None
    return str(dtype)


def parse_file(filename: str, content: bytes) -> pd.DataFrame:
    """پارس CSV/Excel از بایت‌ها (قبلاً _parse_file). رفتار و پیام خطا همان قبلی است."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "csv":
        try:
            return pd.read_csv(io.BytesIO(content))
        except Exception as e:
            raise ValueError(f"خطا در پارس CSV: {e}")
    elif ext in ("xlsx", "xls"):
        try:
            return pd.read_excel(io.BytesIO(content), engine="openpyxl")
        except Exception as e:
            raise ValueError(f"خطا در پارس Excel: {e}")
    else:
        raise ValueError("فرمت فایل باید CSV یا Excel باشد")


def serialize_dataframe_to_csv(df: pd.DataFrame) -> bytes:
    """سریال‌سازی DataFrame به CSV برای ماندگاری در FileStorage (فاز ۲.۱).

    - index ذخیره نمی‌شود (ستون‌های df همان ستون‌های واقعی می‌مانند)
    - None/NaN به رشته خالی نگاشت می‌شود؛ پارس مجدد آن‌ها را NaN می‌کند
    - خروجی UTF-8 — همان قالبی که parse_file برای csv انتظار دارد
    """
    return df.to_csv(index=False).encode("utf-8")


def persist_columns(db: Session, *, data_source_id, organization_id, df: pd.DataFrame) -> None:
    """متادیتای ستون‌ها را در data_source_columns ماندگار می‌کند (قبلاً _persist_columns)."""
    for position, name in enumerate(df.columns.astype(str)):
        db.add(
            DataSourceColumn(
                data_source_id=data_source_id,
                organization_id=organization_id,
                name=name,
                position=position,
                dtype=infer_dtype(df[name].dtype),
            )
        )


def load_dataframe(db: Session, *, ds: DataSource, organization_id, storage: FileStorage) -> pd.DataFrame:
    """بازیابی DataFrame از storage ماندگار (bytea → parse) — قبلاً _load_dataframe.

    - آپلودها: dispatch بر اساس file_type (از پسوند نام فایل می‌آید) — رفتار همان قبلی
    - منابع import شده (file_type="postgres"): محتوا همیشه CSV سریال‌شده
      serialize_dataframe_to_csv است — مستقیم به‌صورت CSV پارس می‌شود
      (نام فایل آن‌ها پسوند ندارد؛ dispatch بر اساس file_type است)
    """
    content = storage.load(db, data_source_id=ds.id, organization_id=organization_id)
    if ds.file_type == "csv":
        try:
            return pd.read_csv(io.BytesIO(content))
        except Exception as e:
            raise ValueError(f"خطا در پارس CSV: {e}")
    if ds.file_type in ("xlsx", "xls"):
        try:
            return pd.read_excel(io.BytesIO(content), engine="openpyxl")
        except Exception as e:
            raise ValueError(f"خطا در پارس Excel: {e}")
    if ds.file_type == "postgres":
        # فاز ۲.۲: import bridge همیشه CSV ذخیره می‌کند — همان قرارداد load
        try:
            return pd.read_csv(io.BytesIO(content))
        except Exception as e:
            raise ValueError(f"خطا در پارس CSV: {e}")
    return parse_file(ds.name, content)


def preview_records(df: pd.DataFrame, rows: int) -> list[dict]:
    """ردیف‌های preview با سریال‌سازی Timestamp/NaN → JSON-safe (قبلاً inline در upload)."""
    preview = df.head(rows).fillna("").to_dict(orient="records")
    for row in preview:
        for k, v in list(row.items()):
            if isinstance(v, (pd.Timestamp, datetime)):
                row[k] = v.isoformat()
            elif isinstance(v, float) and pd.isna(v):
                row[k] = None
    return preview
