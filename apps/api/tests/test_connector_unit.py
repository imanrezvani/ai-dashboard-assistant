"""فاز ۲.۲ گام ۲ — تست‌های unit connector (بدون دیتابیس خارجی)

پوشش: ساخت PostgresConnector، clamp سقف ردیف، اعتبارسنجی نام جدول
(Identifier امن — فقط جدول‌های discover شده)، عدم افشای اعتبارنامه در خطاها،
رفتار factory برای engineهای پشتیبانی‌نشده، و sanitized بودن خطاهای رمزنگاری.
RLS واقعی در تست‌های PostgreSQL (test_data_sources.py) آزموده می‌شود — اینجا fake نمی‌شود.
"""

import uuid

import pytest

from app.connectors import (
    MAX_FETCH_ROWS,
    MAX_SAMPLE_ROWS,
    ConnectionCheck,
    ConnectorError,
    DatabaseConnector,
    PostgresConnector,
    TableInfo,
    get_connector,
)
from app.core.credentials import encrypt_secret
from app.models.database_connection import DatabaseConnection


def _make_conn(**overrides) -> DatabaseConnection:
    defaults = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="ext-pg",
        engine="postgresql",
        host="external.example.com",
        port=5432,
        database_name="analytics",
        username="reader",
        encrypted_password=encrypt_secret("super-secret-password"),
        ssl_mode=None,
        enabled=True,
        status="unknown",
    )
    defaults.update(overrides)
    return DatabaseConnection(**defaults)


# ---------- ساخت / factory ----------

def test_connector_construction_from_model():
    conn = _make_conn()
    connector = PostgresConnector(conn)
    assert isinstance(connector, DatabaseConnector)
    assert isinstance(connector, PostgresConnector)


def test_factory_returns_postgres_connector_for_postgresql():
    connector = get_connector(_make_conn())
    assert isinstance(connector, PostgresConnector)


def test_factory_rejects_unknown_engine():
    conn = _make_conn(engine="mysql")
    with pytest.raises(ConnectorError):
        get_connector(conn)


def test_connector_does_not_hold_plaintext_password():
    conn = _make_conn()
    connector = PostgresConnector(conn)
    assert "super-secret-password" not in repr(connector)
    assert "super-secret-password" not in str(connector)
    # خود مدل هم plaintext ندارد
    assert "password" not in conn.__dict__


# ---------- سقف ردیف (clamp) ----------

def test_sample_rows_limit_clamped_to_max(monkeypatch):
    captured = {}

    def fake_select(self, table, limit):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(PostgresConnector, "_select_rows", fake_select)
    PostgresConnector(_make_conn()).sample_rows("t", limit=10_000_000)
    assert captured["limit"] == MAX_SAMPLE_ROWS


def test_fetch_dataframe_limit_clamped_to_max(monkeypatch):
    captured = {}

    def fake_select(self, table, limit):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(PostgresConnector, "_select_rows", fake_select)
    PostgresConnector(_make_conn()).fetch_dataframe("t", limit=10_000_000)
    assert captured["limit"] == MAX_FETCH_ROWS


def test_sample_rows_minimum_limit_is_one(monkeypatch):
    captured = {}

    def fake_select(self, table, limit):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(PostgresConnector, "_select_rows", fake_select)
    PostgresConnector(_make_conn()).sample_rows("t", limit=0)
    assert captured["limit"] == 1


# ---------- اعتبارسنجی نام جدول (Identifier امن) ----------

def test_unknown_table_rejected_before_any_sql(monkeypatch):
    """جدولی که در discover_tables نیست هرگز به SQL نمی‌رسد."""
    connector = PostgresConnector(_make_conn())
    monkeypatch.setattr(
        connector,
        "discover_tables",
        lambda: [TableInfo(schema="public", name="allowed")],
    )
    executed = []
    monkeypatch.setattr(connector, "_select_rows", lambda t, l: executed.append((t, l)) or [])
    with pytest.raises(ConnectorError, match="table not found"):
        connector._resolve_table("secret_table")
    assert executed == []


def test_known_table_resolves_to_safe_identifier(monkeypatch):
    connector = PostgresConnector(_make_conn())
    monkeypatch.setattr(
        connector,
        "discover_tables",
        lambda: [TableInfo(schema="public", name="orders")],
    )
    ident = connector._resolve_table("orders")
    rendered = ident.as_string(None)  # context=None فقط رندر متنی
    assert '"orders"' in rendered
    assert '"public"' in rendered
    # بدون interpolation خام
    assert "orders" in rendered and rendered.count("'") == 0


def test_schema_qualified_table_accepted(monkeypatch):
    connector = PostgresConnector(_make_conn())
    monkeypatch.setattr(
        connector,
        "discover_tables",
        lambda: [TableInfo(schema="sales", name="orders")],
    )
    ident = connector._resolve_table("sales.orders")
    rendered = ident.as_string(None)
    assert '"sales"."orders"' in rendered


# ---------- خطاهای sanitized ----------

def test_decrypt_failure_is_sanitized_without_content(monkeypatch):
    """ciphertext خراب → ConnectorError عمومی؛ هیچ محتوایی بیرون نمی‌آید."""
    conn = _make_conn(encrypted_password=b"not-valid-fernet-token")
    connector = PostgresConnector(conn)
    check = connector.test_connection()
    assert isinstance(check, ConnectionCheck)
    assert check.ok is False
    assert "could not be decrypted" in check.error
    assert "not-valid-fernet-token" not in (check.error or "")
    assert "super-secret-password" not in (check.error or "")


def test_connection_check_success_shape():
    check = ConnectionCheck(ok=True, latency_ms=12)
    assert check.ok is True and check.latency_ms == 12 and check.error is None
