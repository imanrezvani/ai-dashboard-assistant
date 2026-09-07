"""پشتیبان PostgreSQL (bytea) از FileStorage — فاز ۲.۱

ذخیره در جدول data_source_files (ستون content از نوع bytea/LargeBinary).

امنیت tenant:
  - organization_id ستون اجباری ردیف است و در WHERE همه کوئری‌ها می‌آید
    (defense-in-depth علاوه بر RLS ENABLE+FORCE+Fail-Closed روی همان جدول)
  - یافتن فایل org دیگر با data_source_id جعلی → FileNotFound (نه داده)
  - درج بدون tenant context در PostgreSQL با WITH CHECK پالیسی RLS رد می‌شود

حذف فایل همراه حذف data_source از طریق FK ON DELETE CASCADE انجام می‌شود؛
delete() اختیاری است برای موارد صریح (map مجدد با کانتنات متفاوت و ...).
"""

import uuid

from sqlalchemy.orm import Session

from app.core.storage import FileNotFound, FileStorage, StoredFile
from app.models.data_source_file import DataSourceFile


class PostgresFileStorage(FileStorage):
    """ذخیره فایل به‌صورت bytea در PostgreSQL — ساده، ماندگار، tenant-safe."""

    def save(
        self,
        db: Session,
        *,
        data_source_id: uuid.UUID,
        organization_id: uuid.UUID,
        content: bytes,
        content_type: str | None = None,
    ) -> StoredFile:
        row = DataSourceFile(
            data_source_id=data_source_id,
            organization_id=organization_id,
            content=content,
            content_type=content_type,
            size_bytes=len(content),
        )
        db.add(row)
        return StoredFile(
            data_source_id=data_source_id,
            organization_id=organization_id,
            size_bytes=len(content),
        )

    def load(self, db: Session, *, data_source_id: uuid.UUID, organization_id: uuid.UUID) -> bytes:
        row = (
            db.query(DataSourceFile)
            .filter(
                DataSourceFile.data_source_id == data_source_id,
                DataSourceFile.organization_id == organization_id,  # defense-in-depth کنار RLS
            )
            .first()
        )
        if row is None:
            raise FileNotFound(f"فایل برای data_source {data_source_id} در این سازمان یافت نشد")
        return row.content

    def delete(self, db: Session, *, data_source_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        db.query(DataSourceFile).filter(
            DataSourceFile.data_source_id == data_source_id,
            DataSourceFile.organization_id == organization_id,
        ).delete()
