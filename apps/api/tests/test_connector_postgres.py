"""فاز ۲.۲ گام ۲ — تست‌های integration connector روی PostgreSQL واقعی

دو جنبه واقعی (بدون fake):
  ۱) سمت app-DB (RLS): ردیف DatabaseConnection سازمان‌محور روی دیتابیس تست با
     role اپ NOBYPASSRLS — org دیگر ردیف را نمی‌بیند؛ پس connector هرگز به
     اعتبارنامه org دیگر دسترسی ندارد (decrypt رخ نمی‌دهد).
  ۲) سمت external-DB: یک scratch database واقعی (`tasmim_external_test`) با role
     فقط-خواندنی `connx_reader` — اتصال موفق، discovery، sample، DataFrame،
     سقف ردیف، statement_timeout (با قفل واقعی ACCESS EXCLUSIVE)، read-only
     session و خطاهای sanitized — همه روی سرور واقعی.

ایزولیشن (درس گیت فاز ۲.۱): seed این ماژول در fixture (زمان اجرای تست) انجام
می‌شود نه import، و cleanup فقط ردیف‌های خود ماژول است (slug/email/name با
پیشوند connx) تا هم‌اجرایی با test_data_sources.py در یک session pytest سالم بماند.
"""

import os
import pathlib
import sys
import time
import uuid

API_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

TASMIM_APP_PASSWORD = os.getenv("TASMIM_APP_DB_PASSWORD", "tasmim_app_secret_dev_only")
# اعتبارنامه role فقط-خواندنیِ scratch external — فقط برای تست (قرارداد dev-only موجود)
CONNX_READER_PASSWORD = "connx_secret_dev_only"
EXT_DB_NAME = "tasmim_external_test"
EXT_ROLE = "connx_reader"

import pytest
import psycopg
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.security import hash_password
from app.core.credentials import decrypt_secret, encrypt_secret
from app.connectors import (
    MAX_SAMPLE_ROWS,
    ConnectorError,
    ConnectorTimeout,
    PostgresConnector,
    get_connector,
)
from app.models import Membership, Organization, User
from app.models.database_connection import DatabaseConnection


def _make_app_url(base_url: str, user: str, pwd: str) -> str:
    import urllib.parse as up
    p = up.urlparse(base_url)
    netloc = f"{user}:{pwd}@{p.hostname}"
    if p.port:
        netloc += f":{p.port}"
    return up.urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def _candidate_urls():
    env_test = os.getenv("TEST_DATABASE_URL")
    env_admin = os.getenv("ADMIN_DATABASE_URL")
    if env_test:
        # اولویت با role اپ است (RLS واقعی؛ superuser آن را bypass می‌کند)
        if "tasmim_app" in env_test:
            yield env_test
        try:
            yield _make_app_url(env_test, "tasmim_app", TASMIM_APP_PASSWORD)
        except Exception:
            pass
        yield env_test
    if env_admin:
        try:
            yield _make_app_url(env_admin, "tasmim_app", TASMIM_APP_PASSWORD)
        except Exception:
            pass
        yield env_admin
    yield f"postgresql+psycopg://tasmim_app:{TASMIM_APP_PASSWORD}@localhost:5433/tasmim_yar_test"
    yield "postgresql+psycopg://tasmim:tasmim_secret@localhost:5433/tasmim_yar_test"
    yield f"postgresql+psycopg://tasmim_app:{TASMIM_APP_PASSWORD}@localhost:5432/tasmim_yar"
    yield "postgresql+psycopg://tasmim:tasmim_secret@localhost:5432/tasmim_yar"


def _get_test_engine():
    last_err = None
    for url in _candidate_urls():
        try:
            eng = create_engine(url, pool_pre_ping=True)
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
            return eng, url
        except Exception as e:
            last_err = e
            continue
    pytest.skip(
        f"PostgreSQL در دسترس نیست ({last_err}); نیاز به docker compose -f docker-compose.test.yml up -d",
        allow_module_level=True,
    )


engine, used_url = _get_test_engine()
is_app_user = "tasmim_app" in used_url
Base.metadata.create_all(bind=engine)

# RLS برای database_connections — همان DDL اپ (app/main.py)؛ idempotent و بدون دستکاری داده
with engine.begin() as conn:
    conn.execute(text("ALTER TABLE database_connections ENABLE ROW LEVEL SECURITY"))
    conn.execute(text("ALTER TABLE database_connections FORCE ROW LEVEL SECURITY"))
    conn.execute(text("DROP POLICY IF EXISTS tenant_isolation ON database_connections"))
    conn.execute(text(
        "CREATE POLICY tenant_isolation ON database_connections FOR ALL "
        "USING (organization_id::text = current_setting('app.current_org_id', true)) "
        "WITH CHECK (organization_id::text = current_setting('app.current_org_id', true))"
    ))

# expire_on_commit=False: پس از commit، attributeهای ردیف باید در دسترس بمانند —
# refresh پس از commit زیر RLS fail-closed ممکن نیست (context از بین می‌رود)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


# ---------- superuser engine برای seed و scratch external ----------

def _get_superuser_engine():
    """superuser `tasmim` روی **همان دیتابیس تست** (نه maintenance DB `postgres`) —
    برای seed و cleanup که باید RLS را bypass کنند ولی همان جدول‌ها را ببینند."""
    candidates = []
    # همان host/port/dbname اتصال اصلی، فقط کاربر/رمز superuser
    try:
        candidates.append(_make_app_url(used_url, "tasmim", "tasmim_secret"))
    except Exception:
        pass
    candidates.append("postgresql+psycopg://tasmim:tasmim_secret@localhost:5433/tasmim_yar_test")
    candidates.append("postgresql+psycopg://tasmim:tasmim_secret@localhost:5432/tasmim_yar")
    for url in candidates:
        try:
            se = create_engine(url, pool_pre_ping=True)
            with se.connect() as c:
                c.execute(text("SELECT 1"))
            return se
        except Exception:
            continue
    return None


seed_engine = _get_superuser_engine()

ORG_A_SLUG, ORG_B_SLUG = "org-a-connx", "org-b-connx"
USER_A_EMAIL, USER_B_EMAIL = "alice-connx@org-a.test", "bob-connx@org-b.test"


def _cleanup_own_rows():
    """فقط ردیف‌های خود این ماژول — هیچ جدولی به‌طور کامل تخلیه نمی‌شود."""
    if seed_engine is None:
        return
    with seed_engine.begin() as conn:
        # پارامتر bindشده تا % در LIKE به‌طور امن پاس شود (بدون escaping SQLAlchemy)
        conn.execute(text("DELETE FROM database_connections WHERE name LIKE :p"), {"p": "connx-%"})
        conn.execute(text("DELETE FROM memberships WHERE organization_id IN (SELECT id FROM organizations WHERE slug IN (:a, :b))"),
                     {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM organizations WHERE slug IN (:a, :b)"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM users WHERE email IN (:a, :b)"), {"a": USER_A_EMAIL, "b": USER_B_EMAIL})


@pytest.fixture(scope="module")
def orgs():
    """دو org + کاربر/owner + دو ردیف DatabaseConnection برای org A (زمان تست، نه import)."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    _cleanup_own_rows()
    org_a_id, org_b_id = uuid.uuid4(), uuid.uuid4()
    user_a_id, user_b_id = uuid.uuid4(), uuid.uuid4()
    with seed_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email, full_name, hashed_password) VALUES (:i,:e,:f,:h)"),
                     {"i": str(user_a_id), "e": USER_A_EMAIL, "f": "Alice C", "h": hash_password("TestPass123")})
        conn.execute(text("INSERT INTO users (id, email, full_name, hashed_password) VALUES (:i,:e,:f,:h)"),
                     {"i": str(user_b_id), "e": USER_B_EMAIL, "f": "Bob C", "h": hash_password("TestPass123")})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(org_a_id), "n": "Org A ConnX", "s": ORG_A_SLUG})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(org_b_id), "n": "Org B ConnX", "s": ORG_B_SLUG})
        conn.execute(text("INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:i,:u,:o,'owner')"),
                     {"i": str(uuid.uuid4()), "u": str(user_a_id), "o": str(org_a_id)})
        conn.execute(text("INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:i,:u,:o,'owner')"),
                     {"i": str(uuid.uuid4()), "u": str(user_b_id), "o": str(org_b_id)})
    yield {"org_a": org_a_id, "org_b": org_b_id}
    _cleanup_own_rows()


@pytest.fixture(scope="module")
def ext_pg():
    """scratch external PostgreSQL واقعی: دیتابیس + role فقط-خواندنی + جدول‌های نمونه."""
    if seed_engine is None:
        pytest.skip("superuser engine برای ساخت scratch external در دسترس نیست")
    # pg_roles/pg_database کاتالوگ‌های cluster-global هستند — از هر دیتابیسی دیده می‌شوند
    with seed_engine.connect() as c:
        role_exists = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": EXT_ROLE}).fetchone()
        db_exists = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": EXT_DB_NAME}).fetchone()
    # CREATE DATABASE/ROLE نیاز به autocommit دارد — با psycopg مستقیم روی maintenance DB
    # (utility statementها پارامتر bind نمی‌پذیرند → با sql.Literal/Identifier امن کوتیشن می‌شود)
    from psycopg import sql as psql
    conninfo = f"host={_admin_host()} port={_admin_port()} dbname=postgres user=tasmim password=tasmim_secret"
    with psycopg.connect(conninfo, autocommit=True) as ac:
        role_stmt = psql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
            psql.Identifier(EXT_ROLE), psql.Literal(CONNX_READER_PASSWORD))
        alter_stmt = psql.SQL("ALTER ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
            psql.Identifier(EXT_ROLE), psql.Literal(CONNX_READER_PASSWORD))
        ac.execute(alter_stmt if role_exists else role_stmt)
        if not db_exists:
            ac.execute(psql.SQL("CREATE DATABASE {}").format(psql.Identifier(EXT_DB_NAME)))
    # جدول‌ها و grantها داخل scratch DB
    with psycopg.connect(f"host={_admin_host()} port={_admin_port()} dbname={EXT_DB_NAME} user=tasmim password=tasmim_secret", autocommit=True) as ec:
        ec.execute("DROP TABLE IF EXISTS connx_data")
        ec.execute("CREATE TABLE connx_data (id int PRIMARY KEY, label text, amount numeric)")
        ec.execute("INSERT INTO connx_data VALUES (1,'alpha',10.5),(2,'beta',20.0),(3,'gamma',30.25)")
        ec.execute("DROP TABLE IF EXISTS connx_big")
        ec.execute("CREATE TABLE connx_big (id int PRIMARY KEY)")
        ec.execute("INSERT INTO connx_big SELECT generate_series(1, 1005)")
        ec.execute(f"GRANT CONNECT ON DATABASE {EXT_DB_NAME} TO {EXT_ROLE}")
        ec.execute(f"GRANT USAGE ON SCHEMA public TO {EXT_ROLE}")
        ec.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {EXT_ROLE}")
        ec.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {EXT_ROLE}")
    yield {
        "host": _admin_host(),
        "port": _admin_port(),
        "dbname": EXT_DB_NAME,
        "user": EXT_ROLE,
        "password": CONNX_READER_PASSWORD,
    }


def _admin_host() -> str:
    import urllib.parse as up
    return up.urlparse(used_url).hostname or "localhost"


def _admin_port() -> int:
    import urllib.parse as up
    return up.urlparse(used_url).port or 5432


def _admin_hostport() -> str:
    return f"{_admin_host()}:{_admin_port()}"


@pytest.fixture()
def conn_row_ok(orgs, ext_pg):
    """ردیف DatabaseConnection معتبر برای org A — با رمز واقعی role فقط-خواندنی."""
    row = DatabaseConnection(
        organization_id=orgs["org_a"], name="connx-ok", engine="postgresql",
        host=ext_pg["host"], port=ext_pg["port"], database_name=ext_pg["dbname"],
        username=ext_pg["user"], encrypted_password=encrypt_secret(ext_pg["password"]),
    )
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(orgs["org_a"])})
        db.add(row)
        db.commit()
    yield row
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(orgs["org_a"])})
        db.query(DatabaseConnection).filter(DatabaseConnection.id == row.id).delete()
        db.commit()


@pytest.fixture()
def conn_row_bad_password(orgs, ext_pg):
    """ردیف با رمز اشتباه — برای مسیر خطای sanitized."""
    row = DatabaseConnection(
        organization_id=orgs["org_a"], name="connx-bad", engine="postgresql",
        host=ext_pg["host"], port=ext_pg["port"], database_name=ext_pg["dbname"],
        username=ext_pg["user"], encrypted_password=encrypt_secret("definitely-wrong-password"),
    )
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(orgs["org_a"])})
        db.add(row)
        db.commit()
    yield row
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(orgs["org_a"])})
        db.query(DatabaseConnection).filter(DatabaseConnection.id == row.id).delete()
        db.commit()


# ---------- ۱) اتصال موفق واقعی ----------

def test_connection_success_against_real_postgres(conn_row_ok):
    connector = get_connector(conn_row_ok)
    check = connector.test_connection()
    assert check.ok is True, f"expected ok, got error: {check.error}"
    assert check.error is None
    assert check.latency_ms is not None and check.latency_ms >= 0
    # هیچ رمزی در نتیجه نیست
    assert "connx_secret_dev_only" not in repr(check)


# ---------- ۲) اعتبارنامه اشتباه / خطای اتصال sanitized ----------

def test_bad_password_sanitized_without_leak(conn_row_bad_password):
    connector = PostgresConnector(conn_row_bad_password)
    check = connector.test_connection()
    assert check.ok is False
    assert check.error is not None
    # sanitized: بدون رمز، بدون conninfo، بدون جزئیات درایور
    assert "definitely-wrong-password" not in check.error
    assert "connx_secret_dev_only" not in check.error
    assert "password=" not in check.error.lower()
    assert check.error in {
        "connection failed",
        "external database error",
        "permission denied on external database",
        "connection timed out",
    }


def test_unreachable_host_fails_fast_and_sanitized(orgs):
    row = DatabaseConnection(
        organization_id=orgs["org_a"], name="connx-unreachable", engine="postgresql",
        host="127.0.0.1", port=1, database_name="x", username="u",
        encrypted_password=encrypt_secret("p"),
    )
    connector = PostgresConnector(row, connect_timeout=2)
    check = connector.test_connection()
    assert check.ok is False
    assert "p" != (check.error or "p")  # هیچ محتوای حساس
    assert check.error in {"connection failed", "connection timed out"}


def test_blackhole_host_bounded_by_connect_timeout(orgs):
    """host غیرقابل‌مسیر — call باید در حد connect_timeout محدود بماند."""
    row = DatabaseConnection(
        organization_id=orgs["org_a"], name="connx-blackhole", engine="postgresql",
        host="10.255.255.1", port=5432, database_name="x", username="u",
        encrypted_password=encrypt_secret("p"),
    )
    connector = PostgresConnector(row, connect_timeout=2)
    start = time.monotonic()
    check = connector.test_connection()
    elapsed = time.monotonic() - start
    assert check.ok is False
    assert elapsed < 30, f"connect_timeout باید call را محدود کند؛ {elapsed:.1f}s طول کشید"
    assert "p" not in (check.error or "p")


# ---------- ۳) discovery ----------

def test_discover_tables_lists_user_tables(conn_row_ok):
    connector = get_connector(conn_row_ok)
    tables = connector.discover_tables()
    names = {t.qualified_name for t in tables}
    assert "public.connx_data" in names
    assert "public.connx_big" in names
    # جداول سیستم هرگز نباید باشند
    assert not any(n.startswith(("pg_catalog.", "information_schema.")) for n in names)


# ---------- ۴) sample_rows ----------

def test_sample_rows_returns_structured_rows(conn_row_ok):
    connector = get_connector(conn_row_ok)
    rows = connector.sample_rows("connx_data", 10)
    assert len(rows) == 3
    assert rows[0] == {"id": 1, "label": "alpha", "amount": pytest.approx(10.5)}
    assert set(rows[0].keys()) == {"id", "label", "amount"}


def test_sample_rows_respects_row_limit_cap(conn_row_ok):
    """1005 ردیف در جدول؛ درخواست بی‌نهایت → دقیقاً MAX_SAMPLE_ROWS."""
    connector = get_connector(conn_row_ok)
    rows = connector.sample_rows("connx_big", 10_000_000)
    assert len(rows) == MAX_SAMPLE_ROWS


def test_sample_rows_unknown_table_rejected(conn_row_ok):
    connector = get_connector(conn_row_ok)
    with pytest.raises(ConnectorError, match="table not found"):
        connector.sample_rows("no_such_table_connx", 5)


def test_sample_rows_injection_style_table_name_rejected(conn_row_ok):
    """نام جدول با کاراکتر خطرناک — هرگز به SQL نمی‌رسد (resolution قبل از اجرا)."""
    connector = get_connector(conn_row_ok)
    for malicious in ("connx_data; DROP TABLE connx_data", "connx_data--", '"connx_data"'):
        with pytest.raises(ConnectorError, match="table not found"):
            connector.sample_rows(malicious, 5)


# ---------- ۵) statement_timeout ----------

def test_statement_timeout_aborts_blocked_read(conn_row_ok, ext_pg):
    """قفل ACCESS EXCLUSIVE در session دیگر → SELECT منتظر قفل می‌ماند →
    statement_timeout کوچک کوئری را قطع می‌کند → ConnectorTimeout."""
    connector = get_connector(conn_row_ok)  # statement_timeout پیش‌فرض 15s
    connector._statement_timeout = 1.0  # 1s — کافی برای قفل
    admin = psycopg.connect(
        f"host={ext_pg['host']} port={ext_pg['port']} dbname={ext_pg['dbname']} user=tasmim password=tasmim_secret"
    )
    try:
        with admin.transaction():
            admin.execute("LOCK TABLE connx_big IN ACCESS EXCLUSIVE MODE")
            start = time.monotonic()
            with pytest.raises(ConnectorTimeout):
                connector.sample_rows("connx_big", 10)
            elapsed = time.monotonic() - start
            assert 0.5 <= elapsed < 15, f"statement_timeout باید ~1s قطع کند؛ {elapsed:.1f}s"
    finally:
        admin.close()


# ---------- ۶) fetch_dataframe ----------

def test_fetch_dataframe_returns_pandas_frame(conn_row_ok):
    import pandas as pd
    connector = get_connector(conn_row_ok)
    df = connector.fetch_dataframe("connx_data", 100)
    assert isinstance(df, pd.DataFrame)
    assert df.shape == (3, 3)
    assert list(df.columns) == ["id", "label", "amount"]
    assert df["label"].tolist() == ["alpha", "beta", "gamma"]


def test_fetch_dataframe_limit_clamped(conn_row_ok):
    connector = get_connector(conn_row_ok)
    df = connector.fetch_dataframe("connx_big", 10_000_000)
    assert len(df) == 1005  # کل جدول کمتر از MAX_FETCH_ROWS است؛ سقف 100_000


# ---------- ۷) read-only enforcement ----------

def test_connector_api_has_no_write_methods(conn_row_ok):
    connector = get_connector(conn_row_ok)
    for verb in ("execute", "execute_sql", "raw_sql", "run", "query", "insert",
                 "update", "delete", "drop", "create_table", "truncate", "exec"):
        assert not hasattr(connector, verb), f"connector نباید متد write به نام {verb} داشته باشد"


def test_session_is_read_only_write_attempt_fails(conn_row_ok):
    """حتی با دسترسی مستقیم به session داخلی، نوشتن در سطح transaction رد می‌شود
    (default_transaction_read_only=on) — مستقل از grantهای role."""
    connector = get_connector(conn_row_ok)
    raw = connector._connect()
    try:
        with raw.cursor() as cur:
            with pytest.raises(psycopg.Error) as excinfo:
                cur.execute("INSERT INTO connx_data VALUES (99, 'evil', 1)")
            msg = str(excinfo.value).lower()
            assert "read-only" in msg or "permission denied" in msg
    finally:
        raw.rollback()
        raw.close()


def test_show_transaction_read_only_is_on(conn_row_ok):
    connector = get_connector(conn_row_ok)
    raw = connector._connect()
    try:
        cur = raw.execute("SHOW transaction_read_only")
        val = cur.fetchone()
        assert val is not None and list(val.values())[0] == "on"
    finally:
        raw.rollback()
        raw.close()


# ---------- ۸) credential non-disclosure در کل مسیر ----------

def test_no_plaintext_in_connector_state_or_errors(conn_row_ok, ext_pg):
    connector = get_connector(conn_row_ok)
    state = repr(connector.__dict__)
    assert ext_pg["password"] not in state
    try:
        connector.sample_rows("no_such_connx_table", 1)
    except ConnectorError as exc:
        assert ext_pg["password"] not in str(exc)
    try:
        connector.sample_rows("connx_data", 10)
    except ConnectorError:
        pass  # مسیر موفق — استثنا نیست


def test_decrypt_only_happens_inside_connector(orgs, ext_pg):
    """ciphertext با decrypt_secret فقط در مسیر connector باز می‌شود؛ خود ردیف هرگز plaintext ندارد."""
    row = DatabaseConnection(
        organization_id=orgs["org_a"], name="connx-cipher", engine="postgresql",
        host=ext_pg["host"], port=ext_pg["port"], database_name=ext_pg["dbname"],
        username=ext_pg["user"], encrypted_password=encrypt_secret(ext_pg["password"]),
    )
    assert row.encrypted_password != ext_pg["password"].encode()
    assert decrypt_secret(row.encrypted_password) == ext_pg["password"]
    # مدل هیچ attribute به نام password ندارد
    assert not hasattr(row, "password")


# ---------- ۹) cross-org: RLS جلوی دسترسی را می‌گیرد ----------

def test_cross_org_connection_row_invisible(conn_row_ok, orgs):
    """org B ردیف اتصال org A را نمی‌بیند → connector هرگز با اعتبارنامه org دیگر
    ساخته نمی‌شود (decrypt فقط پس از RLS-scoped fetch رخ می‌دهد)."""
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(orgs["org_b"])})
        assert db.query(DatabaseConnection).count() == 0, "org B نباید هیچ اتصالی ببیند"
        db.rollback()
    # و در context درست، org A ردیف خودش را می‌بیند
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(orgs["org_a"])})
        seen = db.query(DatabaseConnection).filter(DatabaseConnection.name == "connx-ok").one()
        assert str(seen.organization_id) == str(orgs["org_a"])
        db.rollback()


def test_no_context_fail_closed(conn_row_ok):
    """بدون tenant context، هیچ ردیف اتصالی دیده نمی‌شود (fail-closed)."""
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', '', true)"))
        assert db.query(DatabaseConnection).count() == 0
        db.rollback()
