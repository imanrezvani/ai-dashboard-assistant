"""لایه انتزاعی storage برای فایل‌های آپلودی — فاز ۲.۱

جایگزین PENDING_UPLOADS (dict حافظه‌ای در router) با یک interface تمیز:
  - `save`: بایت‌های فایل را ماندگار می‌کند و کلید/شناسه ذخیره برمی‌گرداند
  - `load`: بایت‌ها را برای یک (organization_id, data_source_id) برمی‌گرداند
  - `delete`: حذف منطقی همراه data_source

قواعد امنیتی (غیرقابل نقض در همه backendها):
  - هر عملیات با organization_id صدا زده می‌شود؛ هیچ APIای بدون tenant context وجود ندارد
  - در backend دیتابیسی، organization_id در WHERE کوئری است → defense-in-depth
    در کنار RLS (ENABLE + FORCE + Fail-Closed) روی همان جدول‌ها
  - خطای یافت‌نشدن = NotFound (نه تفاوت بین «موجود در org دیگر» و «ندارد»)
"""

import uuid
from abc import ABC, abstractmethod


class StorageError(Exception):
    """خطای عمومی لایه storage."""


class FileNotFound(StorageError):
    """فایل برای این tenant/data_source وجود ندارد (یا به org دیگری تعلق دارد)."""


class StoredFile:
    """متادیتای خروجی save — بدون محتوای باینری."""

    __slots__ = ("data_source_id", "organization_id", "size_bytes")

    def __init__(self, data_source_id: uuid.UUID, organization_id: uuid.UUID, size_bytes: int) -> None:
        self.data_source_id = data_source_id
        self.organization_id = organization_id
        self.size_bytes = size_bytes


class FileStorage(ABC):
    """انتزاع ذخیره‌سازی فایل آپلودی. همه backendها باید tenant-safe باشند."""

    @abstractmethod
    def save(
        self,
        db,
        *,
        data_source_id: uuid.UUID,
        organization_id: uuid.UUID,
        content: bytes,
        content_type: str | None = None,
    ) -> StoredFile:
        """بایت‌ها را برای (organization_id, data_source_id) ذخیره می‌کند."""

    @abstractmethod
    def load(self, db, *, data_source_id: uuid.UUID, organization_id: uuid.UUID) -> bytes:
        """بایت‌های فایل را برمی‌گرداند؛ اگر وجود نداشت یا به org دیگری بود → FileNotFound."""

    @abstractmethod
    def delete(self, db, *, data_source_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        """حذف فایل (در backend دیتابیسی از FK CASCADE هم پوشش داده می‌شود)."""
