"""انتزاع connector دیتابیس خارجی — فاز ۲.۲ گام ۲ (docs/PHASE2_PLAN.md §4)

این ABC تنها مسیر مجاز دسترسی به دیتابیس خارجی است:
  - هیچ روشی SQL خام نمی‌پذیرد (هرگز arbitrary SQL وجود ندارد)
  - همه عملیات read-only / SELECT-only هستند
  - هر پیاده‌سازی باید timeout اتصال، statement_timeout و سقف ردیف را اعمال کند
  - خطاها فقط به‌صورت sanitized (ConnectorError) بیرون می‌آیند — بدون اعتبارنامه/DSN

پیاده‌سازی فاز ۲.۲: فقط PostgreSQL (postgres.py). موتورهای دیگر عمداً پیاده
نمی‌شوند (§11)؛ factory برای آن‌ها ConnectorError می‌دهد.
"""

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

from app.models.database_connection import DatabaseConnection


class ConnectorError(Exception):
    """خطای عمومی لایه connector — پیام sanitized و امن برای نمایش (بدون رمز/DSN)."""


class ConnectorTimeout(ConnectorError):
    """عملیات در مهلت تعیین‌شده کامل نشد (اتصال یا اجرای کوئری)."""


class ConnectorReadOnlyViolation(ConnectorError):
    """تلاش برای عملیات نوشتن از طریق connector — هرگز مجاز نیست."""


class ConnectionCheck:
    """نتیجه sanitized تست اتصال — بدون هیچ اطلاعات اعتبارنامه."""

    __slots__ = ("ok", "latency_ms", "error")

    def __init__(self, ok: bool, latency_ms: int | None = None, error: str | None = None) -> None:
        self.ok = ok
        self.latency_ms = latency_ms
        self.error = error  # فقط متن sanitized؛ هرگز DSN/رمز/جزئیات درایور


class TableInfo:
    """متادیتای امن جدول خارجی (schema-qualified) — بدون هیچ داده."""

    __slots__ = ("schema", "name")

    def __init__(self, schema: str, name: str) -> None:
        self.schema = schema
        self.name = name

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


class DatabaseConnector(ABC):
    """پروتکل مشترک همه connectorها. فقط عملیات read-only."""

    @abstractmethod
    def test_connection(self) -> ConnectionCheck:
        """برقراری اتصال با connect_timeout و اجرای SELECT 1؛ نتیجه sanitized."""

    @abstractmethod
    def discover_tables(self) -> list[TableInfo]:
        """فهرست جدول‌های user-visible دیتابیس خارجی (از catalog، بدون داده)."""

    @abstractmethod
    def sample_rows(self, table: str, limit: int) -> list[dict[str, Any]]:
        """SELECT با Identifier امن، LIMIT سقف‌دار و statement_timeout."""

    @abstractmethod
    def fetch_dataframe(self, table: str, limit: int) -> pd.DataFrame:
        """همان SELECT به‌صورت DataFrame برای import bridge (گام ۳)."""


def get_connector(conn: DatabaseConnection) -> DatabaseConnector:
    """Factory بر اساس engine ردیف DatabaseConnection (بعد از RBAC+RLS)."""
    # import داخل تابع تا وابستگی دایره‌ای با postgres.py شکل نگیرد
    from app.connectors.postgres import PostgresConnector

    if conn.engine == "postgresql":
        return PostgresConnector(conn)
    raise ConnectorError(f"unsupported database engine: {conn.engine}")
