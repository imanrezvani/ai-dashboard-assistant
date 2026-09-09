"""connector PostgreSQL خارجی — فاز ۲.۲ گام ۲ (docs/PHASE2_PLAN.md §5)

مرز امنیتی این ماژول (غیرقابل نقض):
  - فقط SELECT / catalog — هیچ مسیر SQL خام وجود ندارد و هیچ متدی متن SQL نمی‌پذیرد
  - شناسه‌های جدول/schema فقط با psycopg.sql.Identifier کوتیشن می‌شوند؛ هرگز interpolation
  - SELECT 1 با connect_timeout؛ کوئری‌ها با statement_timeout
  - نشست read-only: default_transaction_read_only=on → نوشتن حتی با SQL دستی هم fail می‌شود
  - سقف ردیف: MAX_SAMPLE_ROWS / MAX_FETCH_ROWS — limit ورودی به این سقف‌ها clamp می‌شود
  - `table` فقط پس از تطبیق با خروجی discover_tables پذیرفته می‌شود (name یا schema.name)
  - رمز فقط اینجا، فقط پس از RBAC+RLS (factory از ردیف سازمان‌محور) decrypt می‌شود
  - هیچ رمز/DSN/کوئری‌سازی لاگ نمی‌شود؛ خطاها به ConnectorError sanitized تبدیل می‌شوند
"""

import time
from typing import Any

import pandas as pd
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.connectors.base import (
    ConnectionCheck,
    ConnectorError,
    ConnectorTimeout,
    DatabaseConnector,
    TableInfo,
)
from app.core.credentials import CredentialError, decrypt_secret
from app.models.database_connection import DatabaseConnection

# سقف‌های امنیتی — limit ورودی هرگز نمی‌تواند از این‌ها بگذرد (§5)
MAX_SAMPLE_ROWS = 1_000
MAX_FETCH_ROWS = 100_000

# مهلت‌های پیش‌فرض (ثانیه) — سازنده قابل override است تا تست‌ها قطعی باشند
DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_STATEMENT_TIMEOUT_S = 15.0

# catalog: فقط جدول‌های user-visible (نه سیستم، نه view/foreign) — §6/§7
_TABLES_QUERY = (
    "SELECT n.nspname AS schema, c.relname AS name "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind = 'r' "
    "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg_toast%' "
    "AND n.nspname NOT LIKE 'pg_temp%' "
    "ORDER BY n.nspname, c.relname"
)


def _sanitize_error(exc: Exception) -> str:
    """پیام خطای امن برای ذخیره/نمایش — هرگز conninfo/رمز/جزئیات داخلی درایور."""
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return "query timed out"
    if isinstance(exc, psycopg.OperationalError):
        return "connection failed"  # OperationalError می‌تواند conninfo را embed کند
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return "permission denied on external database"
    if isinstance(exc, psycopg.Error):
        return "external database error"
    return "unexpected connector error"


def _looks_like_timeout(exc: psycopg.OperationalError) -> bool:
    msg = str(exc).lower()
    return "timeout" in msg or "timed out" in msg


class PostgresConnector(DatabaseConnector):
    """تنها پیاده‌سازی فاز ۲.۲ — فقط خواندن، فقط شناسه‌های امن."""

    def __init__(
        self,
        conn: DatabaseConnection,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
        statement_timeout: float = DEFAULT_STATEMENT_TIMEOUT_S,
    ) -> None:
        self._conn = conn
        self._connect_timeout = connect_timeout
        self._statement_timeout = statement_timeout

    # ----- اتصال -----

    def _connect(self) -> psycopg.Connection:
        """اتصال read-only با timeout. رمز فقط اینجا decrypt می‌شود.

        kwargs-based (نه conninfo string) تا هیچ رشته‌ای حاوی رمز ساخته نشود؛
        خطای اتصال به ConnectorError/ConnectorTimeout sanitized تبدیل می‌شود.
        """
        try:
            password = decrypt_secret(self._conn.encrypted_password)
        except CredentialError as exc:
            raise ConnectorError("stored credentials could not be decrypted") from exc

        conninfo_kwargs: dict[str, Any] = {
            "host": self._conn.host,
            "port": self._conn.port,
            "dbname": self._conn.database_name,
            "user": self._conn.username,
            "password": password,
            "connect_timeout": max(1, int(self._connect_timeout)),
            # read-only در سطح session — حتی SQL دستی هم نوشتن را رد می‌کند
            "options": (
                f"-c default_transaction_read_only=on "
                f"-c statement_timeout={int(self._statement_timeout * 1000)}"
            ),
        }
        if self._conn.ssl_mode:
            conninfo_kwargs["sslmode"] = self._conn.ssl_mode
        try:
            return psycopg.connect(row_factory=dict_row, **conninfo_kwargs)
        except psycopg.OperationalError as exc:
            if _looks_like_timeout(exc):
                raise ConnectorTimeout("connection timed out") from exc
            raise ConnectorError(_sanitize_error(exc)) from exc
        except psycopg.Error as exc:
            raise ConnectorError(_sanitize_error(exc)) from exc

    # ----- context manager نشست read-only -----

    def _session(self) -> "_Session":
        return _Session(self)

    # ----- DatabaseConnector API -----

    def test_connection(self) -> ConnectionCheck:
        start = time.monotonic()
        try:
            with self._session() as raw:
                with raw.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            latency_ms = int((time.monotonic() - start) * 1000)
            return ConnectionCheck(ok=True, latency_ms=latency_ms)
        except ConnectorError as exc:
            return ConnectionCheck(ok=False, error=str(exc))
        except Exception as exc:  # دفاع آخر — همیشه sanitized
            return ConnectionCheck(ok=False, error=_sanitize_error(exc))

    def discover_tables(self) -> list[TableInfo]:
        try:
            with self._session() as raw:
                with raw.cursor() as cur:
                    cur.execute(_TABLES_QUERY)
                    rows = cur.fetchall()
            return [TableInfo(schema=r["schema"], name=r["name"]) for r in rows]
        except ConnectorError:
            raise
        except Exception as exc:
            raise self._wrap_query_error(exc) from exc

    def sample_rows(self, table: str, limit: int) -> list[dict[str, Any]]:
        clamped = max(1, min(int(limit), MAX_SAMPLE_ROWS))
        return self._select_rows(table, clamped)

    def fetch_dataframe(self, table: str, limit: int) -> pd.DataFrame:
        clamped = max(1, min(int(limit), MAX_FETCH_ROWS))
        rows = self._select_rows(table, clamped)
        return pd.DataFrame.from_records(rows)

    # ----- داخلی -----

    def _resolve_table(self, table: str) -> sql.Composable:
        """`table` (name یا schema.name) → Identifier امن، فقط اگر در discover_tables باشد."""
        known = {(t.schema, t.name) for t in self.discover_tables()}
        if "." in table:
            schema, name = table.split(".", 1)
        else:
            schema, name = "public", table
        if (schema, name) not in known:
            raise ConnectorError("table not found")
        return sql.SQL(".").join([sql.Identifier(schema), sql.Identifier(name)])

    def _select_rows(self, table: str, limit: int) -> list[dict[str, Any]]:
        """SELECT * با Identifier امن + LIMIT پارامتری. تنها مسیر بازگشت داده."""
        ident = self._resolve_table(table)
        query = sql.SQL("SELECT * FROM {} LIMIT %s").format(ident)
        try:
            with self._session() as raw:
                with raw.cursor() as cur:
                    cur.execute(query, (limit,))
                    return list(cur.fetchall())
        except ConnectorError:
            raise
        except Exception as exc:
            raise self._wrap_query_error(exc) from exc

    def _wrap_query_error(self, exc: Exception) -> ConnectorError:
        if isinstance(exc, psycopg.errors.QueryCanceled):
            return ConnectorTimeout("query timed out")
        return ConnectorError(_sanitize_error(exc))


class _Session:
    """context manager دور یک connection — قرارداد read-only را یک‌جا نگه می‌دارد."""

    def __init__(self, connector: PostgresConnector) -> None:
        self._connector = connector
        self._raw: psycopg.Connection | None = None

    def __enter__(self) -> psycopg.Connection:
        self._raw = self._connector._connect()
        return self._raw

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._raw is not None:
            try:
                self._raw.rollback()  # همیشه بدون commit بسته می‌شود — هیچ تغییری ممکن نیست
            finally:
                self._raw.close()
        return False
