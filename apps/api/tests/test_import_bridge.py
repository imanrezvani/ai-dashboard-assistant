"""فاز ۲.۲ گام ۴ — تست‌های import bridge روی PostgreSQL واقعی

پوشش (docs/PHASE2_PLAN.md §7 و §9.11):
  - Import موفق: DataSource با file_type="postgres" + database_connection_id + فایل CSV
    ماندگار در data_source_files + متادیتای ستون‌ها؛ سپس map موجود → fact_rows
  - Tenant isolation: cross-org connection id → 404؛ org B هیچ داده‌ای از import org A نمی‌بیند
  - امنیت: رمز plaintext در هیچ پاسخ/خطایی نیست؛ SQL خام پذیرفته نمی‌شود؛
    unsafe identifier → 502 با پیام sanitized؛ limit clamp
  - رفتار خطا: disabled → 400؛ اتصال ناموفق → 502؛ جدول ناموجود → 502؛ جدول خالی → 400؛
    درخواست ناقص → 422
  - Provenance: حذف اتصال → database_connection_id=NULL و DataSource/fact_rows سالم
  - migration: جدول data_sources ستون nullable با FK ON DELETE SET NULL دارد

ایزولیشن (درس گیت‌های فاز ۲.۱/۲.۲): seed در fixture (زمان اجرا)، cleanup فقط ردیف‌های
خود ماژول (پیشوند connx-imp)، بدون drop_all، بدون دستکاری import-time state مشترک.
"""

import io
import os
import pathlib
import sys
import uuid

API_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

TASMIM_APP_PASSWORD = os.getenv("TASMIM_APP_DB_PASSWORD", "tasmim_app_secret_dev_only")
CONNX_READER_PASSWORD = "connx_secret_dev_only"
EXT_DB_NAME = "tasmim_external_test"
EXT_ROLE = "connx_reader"
EXT_SECRET_PLAINTEXT = CONNX_READER_PASSWORD  # هرگز نباید در پاسخ/خطا/log ظاهر شود

import pytest
import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.core.security import create_access_token, hash_password
from app.main import app


def _make_url(base_url: str, user: str, pwd: str) -> str:
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
        if "tasmim_app" in env_test:
            yield env_test
        try:
            yield _make_url(env_test, "tasmim_app", TASMIM_APP_PASSWORD)
        except Exception:
            pass
        yield env_test
    if env_admin:
        try:
            yield _make_url(env_admin, "tasmim_app", TASMIM_APP_PASSWORD)
        except Exception:
            pass
        yield env_admin
    yield f"postgresql+psycopg://tasmim_app:{TASMIM_APP_PASSWORD}@localhost:5433/tasmim_yar_test"
    yield "postgresql+psycopg://tasmim:tasmim_secret@localhost:5433/tasmim_yar_test"


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
Base.metadata.create_all(bind=engine)

# RLS روی جداول داده‌ای (همان الگوی test_db_connections_api.py) — idempotent
with engine.begin() as conn:
    for table in ("data_sources", "data_source_files", "data_source_columns", "fact_rows",
                  "database_connections"):
        conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        conn.execute(text(
            f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
            "USING (organization_id::text = current_setting('app.current_org_id', true)) "
            "WITH CHECK (organization_id::text = current_setting('app.current_org_id', true))"
        ))

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


@pytest.fixture(scope="module", autouse=True)
def _api_client():
    """نصب override روی engine تست این ماژول + TestClient؛ restore پس از ماژول.

    (درس گیت‌های قبلی: هیچ دستکاری import-time روی dependency_overrides مشترک —
    فقط داخل fixture، با capture/restore.)
    """
    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    prev_override = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    if prev_override is not None:
        app.dependency_overrides[get_db] = prev_override
    else:
        app.dependency_overrides.pop(get_db, None)


def _get_superuser_engine():
    candidates = []
    try:
        candidates.append(_make_url(used_url, "tasmim", "tasmim_secret"))
    except Exception:
        pass
    candidates.append("postgresql+psycopg://tasmim:tasmim_secret@localhost:5433/tasmim_yar_test")
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

ORG_A_SLUG, ORG_B_SLUG = "org-a-connx-imp", "org-b-connx-imp"
_MODULE_EMAILS = [
    "alice-connx-imp@org-a.test", "bob-connx-imp@org-b.test",
    "admin-connx-imp@org-a.test", "mgr-connx-imp@org-a.test",
]


def _cleanup_own_rows():
    """حذف فقط ردیف‌های این ماژول (slug/ایمیل/نام اتصال پیشونددار). rerun-safe."""
    if seed_engine is None:
        return
    with seed_engine.begin() as conn:
        conn.execute(text("DELETE FROM data_sources WHERE name LIKE :p"), {"p": "connx-imp-%"})
        conn.execute(text("DELETE FROM database_connections WHERE name LIKE :p"), {"p": "connx-imp-%"})
        conn.execute(text(
            "DELETE FROM memberships WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE slug IN (:a, :b))"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM organizations WHERE slug IN (:a, :b)"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM users WHERE email = ANY(:emails)"), {"emails": _MODULE_EMAILS})


@pytest.fixture(scope="module")
def users():
    """org A: owner/admin/manager — org B: owner (برای cross-org)."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    _cleanup_own_rows()
    ids = {
        "org_a": uuid.uuid4(), "org_b": uuid.uuid4(),
        "owner": uuid.uuid4(), "admin": uuid.uuid4(), "manager": uuid.uuid4(),
        "b_owner": uuid.uuid4(),
    }
    emails = {
        "owner": _MODULE_EMAILS[0], "admin": _MODULE_EMAILS[2],
        "manager": _MODULE_EMAILS[3], "b_owner": _MODULE_EMAILS[1],
    }
    with seed_engine.begin() as conn:
        for key in ("owner", "admin", "manager", "b_owner"):
            conn.execute(text(
                "INSERT INTO users (id, email, full_name, hashed_password) VALUES (:i,:e,:f,:h)"),
                {"i": str(ids[key]), "e": emails[key], "f": "U", "h": hash_password("TestPass123")})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(ids["org_a"]), "n": "Org A Imp", "s": ORG_A_SLUG})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(ids["org_b"]), "n": "Org B Imp", "s": ORG_B_SLUG})
        for key, role in (("owner", "owner"), ("admin", "admin"), ("manager", "manager")):
            conn.execute(text(
                "INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:i,:u,:o,:r)"),
                {"i": str(uuid.uuid4()), "u": str(ids[key]), "o": str(ids["org_a"]), "r": role})
        conn.execute(text(
            "INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:i,:u,:o,'owner')"),
            {"i": str(uuid.uuid4()), "u": str(ids["b_owner"]), "o": str(ids["org_b"])})
    yield ids
    _cleanup_own_rows()


@pytest.fixture(scope="module")
def ext_pg():
    """scratch external PostgreSQL واقعی (همان الگوی test_connector_postgres.py).

    یک جدول داده‌دار (برای import موفق) و یک جدول خالی (برای empty → 400).
    """
    if seed_engine is None:
        pytest.skip("superuser engine برای ساخت scratch external در دسترس نیست")
    with seed_engine.connect() as c:
        role_exists = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": EXT_ROLE}).fetchone()
        db_exists = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": EXT_DB_NAME}).fetchone()
    import urllib.parse as up
    p = up.urlparse(used_url)
    host, port = p.hostname or "localhost", p.port or 5433
    with psycopg.connect(f"host={host} port={port} dbname=postgres user=tasmim password=tasmim_secret", autocommit=True) as ac:
        from psycopg import sql as psql
        role_stmt = psql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
            psql.Identifier(EXT_ROLE), psql.Literal(CONNX_READER_PASSWORD))
        alter_stmt = psql.SQL("ALTER ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
            psql.Identifier(EXT_ROLE), psql.Literal(CONNX_READER_PASSWORD))
        ac.execute(alter_stmt if role_exists else role_stmt)
        if not db_exists:
            ac.execute(psql.SQL("CREATE DATABASE {}").format(psql.Identifier(EXT_DB_NAME)))
    with psycopg.connect(f"host={host} port={port} dbname={EXT_DB_NAME} user=tasmim password=tasmim_secret", autocommit=True) as ec:
        ec.execute("DROP TABLE IF EXISTS connx_imp_data")
        ec.execute("DROP TABLE IF EXISTS connx_imp_empty")
        ec.execute("CREATE TABLE connx_imp_data (id int PRIMARY KEY, label text, amount numeric, day date)")
        ec.execute("INSERT INTO connx_imp_data VALUES "
                   "(1,'alpha',10.5,'2026-01-01'),(2,'beta',20.0,'2026-01-02'),(3,'gamma',30.25,'2026-01-03')")
        ec.execute("CREATE TABLE connx_imp_empty (id int, label text)")
        ec.execute("GRANT CONNECT ON DATABASE " + EXT_DB_NAME + " TO " + EXT_ROLE)
        ec.execute("GRANT USAGE ON SCHEMA public TO " + EXT_ROLE)
        ec.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO " + EXT_ROLE)
    yield {"host": host, "port": port, "dbname": EXT_DB_NAME, "user": EXT_ROLE}


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _token_for(user_id, org_id):
    return create_access_token({"sub": str(user_id), "org_id": str(org_id)})


@pytest.fixture()
def client(_api_client):
    return _api_client


CONN_NAME = "connx-imp-primary"


def _create_conn_via_api(client, users, ext_pg, *, name=CONN_NAME, host=None, password=CONNX_READER_PASSWORD, enabled=True):
    """ساخت اتصال از طریق API واقعی (POST /database-connections) به‌عنوان admin org A."""
    tok_admin = _token_for(users["admin"], users["org_a"])
    payload = {
        "name": name,
        "engine": "postgresql",
        "host": host or ext_pg["host"],
        "port": ext_pg["port"],
        "database_name": ext_pg["dbname"],
        "username": ext_pg["user"],
        "password": password,
    }
    r = client.post("/database-connections", json=payload, headers=_hdr(tok_admin, users["org_a"]))
    assert r.status_code == 201, r.text
    conn = r.json()
    if not enabled:
        r2 = client.post(f"/database-connections/{conn['id']}/test", headers=_hdr(tok_admin, users["org_a"]))
        assert r2.status_code == 200
        # UPDATE با superuser — با app-role، fail-closed RLS بدون context هیچ ردیفی را update نمی‌کند
        with seed_engine.begin() as c:
            c.execute(text("UPDATE database_connections SET enabled=false WHERE id=:i"),
                      {"i": conn["id"]})
    return conn


def _import(client, users, conn_id, *, table="connx_imp_data", limit=10000, name=None, org=None, role="admin"):
    org_id = org if org is not None else users["org_a"]
    tok = _token_for(users[role], org_id)
    body = {"table": table, "limit": limit}
    if name is not None:
        body["name"] = name
    return client.post(f"/database-connections/{conn_id}/import", json=body, headers=_hdr(tok, org_id))


# ---------- ۱) Import موفق + round-trip کامل به fact_rows ----------

def test_import_success_creates_source_and_pipeline(client, users, ext_pg):
    conn = _create_conn_via_api(client, users, ext_pg)
    r = _import(client, users, conn["id"], name="connx-imp-src1")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["file_type"] == "postgres"
    assert body["status"] == "pending"
    assert body["row_count"] == 3
    assert body["name"] == "connx-imp-src1"
    assert body["database_connection_id"] == conn["id"]
    assert body["columns"] == ["id", "label", "amount", "day"]
    # هیچ رمز/DSN در پاسخ نیست
    assert EXT_SECRET_PLAINTEXT not in r.text

    ds_id = uuid.UUID(body["data_source_id"])

    # DataSource ماندگار با provenance — با superuser (RLS-bypass برای verify)
    with seed_engine.connect() as c:
        row = c.execute(text(
            "SELECT database_connection_id, file_type, status, organization_id, row_count "
            "FROM data_sources WHERE id = :i"), {"i": str(ds_id)}).fetchone()
        assert row is not None
        assert str(row.database_connection_id) == conn["id"]
        assert row.file_type == "postgres"
        assert row.status == "pending"
        assert str(row.organization_id) == str(users["org_a"])
        # فایل CSV ماندگار در data_source_files
        f = c.execute(text(
            "SELECT size_bytes, content FROM data_source_files WHERE data_source_id = :i"),
            {"i": str(ds_id)}).fetchone()
        assert f is not None and f.size_bytes > 0
        content = bytes(f.content)
        assert b"alpha" in content and b"2026-01-01" in content
        # متادیتای ستون‌ها
        cols = c.execute(text(
            "SELECT name, position, dtype FROM data_source_columns "
            "WHERE data_source_id = :i ORDER BY position"), {"i": str(ds_id)}).fetchall()
        assert [c2.name for c2 in cols] == ["id", "label", "amount", "day"]
        assert [c2.position for c2 in cols] == [0, 1, 2, 3]
        assert all(c2.dtype for c2 in cols)

    # map موجود → fact_rows (مسیر فاز ۲.۱ — تغییر نکرده)
    tok_admin = _token_for(users["admin"], users["org_a"])
    mr = client.post(f"/data-sources/{ds_id}/map", json={
        "measure_column": "amount", "date_column": "day", "label_column": "label",
    }, headers=_hdr(tok_admin, users["org_a"]))
    assert mr.status_code == 200, mr.text
    assert mr.json()["inserted_rows"] == 3
    assert mr.json()["status"] == "mapped"

    with seed_engine.connect() as c:
        facts = c.execute(text(
            "SELECT measure_value, dimension_date, dimension_label FROM fact_rows "
            "WHERE data_source_id = :i ORDER BY measure_value"), {"i": str(ds_id)}).fetchall()
        assert len(facts) == 3
        assert float(facts[0].measure_value) == 10.5
        assert facts[0].dimension_label == "alpha"
        assert str(facts[0].dimension_date) == "2026-01-01"
        # همه fact_rows با org درست
        org_check = c.execute(text(
            "SELECT count(*) FROM fact_rows WHERE data_source_id = :i "
            "AND organization_id = :o"), {"i": str(ds_id), "o": str(users["org_a"])}).fetchone()
        assert org_check[0] == 3

    # DataSourceOut حالا database_connection_id را نشان می‌دهد
    gr = client.get(f"/data-sources/{ds_id}", headers=_hdr(tok_admin, users["org_a"]))
    assert gr.status_code == 200
    assert gr.json()["database_connection_id"] == conn["id"]


def test_import_duplicate_creates_independent_source(client, users, ext_pg):
    """Import دوباره همان جدول → DataSource جدید مستقل (دو بار map = دو مجموعه fact_rows)."""
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-dup")
    r1 = _import(client, users, conn["id"], table="connx_imp_data", name="connx-imp-dup-a")
    r2 = _import(client, users, conn["id"], table="connx_imp_data", name="connx-imp-dup-b")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["data_source_id"] != r2.json()["data_source_id"]
    ds1 = uuid.UUID(r1.json()["data_source_id"])
    ds2 = uuid.UUID(r2.json()["data_source_id"])
    with seed_engine.connect() as c:
        for ds in (ds1, ds2):
            assert c.execute(text("SELECT count(*) FROM data_source_files WHERE data_source_id=:i"),
                             {"i": str(ds)}).fetchone()[0] == 1


def test_import_limit_layered_enforcement(client, users, ext_pg):
    """دو لایه دفاع سقف ردیف (رفتار قطعی):
      - limit > 1,000,000 (سقف اسکیما) → 422 (oversized request)
      - limit داخل اسکیما اما بالاتر از MAX_FETCH_ROWS (100k) → پذیرفته و در
        connector clamp می‌شود (رفتار clamp خود connector در تست‌های unit پوشش داده شده)
    """
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-clamp")
    r = _import(client, users, conn["id"], limit=250_000, name="connx-imp-clamp-src")
    assert r.status_code == 201, r.text
    assert r.json()["row_count"] == 3  # جدول فقط ۳ ردیف دارد
    # oversized request → 422 قطعی (بدون هیچ ساخت DataSource)
    r2 = _import(client, users, conn["id"], limit=999_999_999)
    assert r2.status_code == 422
    with seed_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM data_sources WHERE name LIKE :p"),
                         {"p": "connx-imp-clamp%"}).fetchone()[0] == 1


# ---------- ۲) Tenant isolation ----------

def test_cross_org_import_returns_404(client, users, ext_pg):
    """org B نمی‌تواند از اتصال org A import کند — 404، بدون افشای وجود."""
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-xorg")
    r = _import(client, users, conn["id"], org=users["org_b"], role="b_owner")
    assert r.status_code == 404
    assert "اتصال" in r.json()["detail"] or "connection" in r.json()["detail"].lower()
    # و هیچ DataSourceای ساخته نشده
    with seed_engine.connect() as c:
        n = c.execute(text(
            "SELECT count(*) FROM data_sources WHERE organization_id = :o AND name LIKE :p"),
            {"o": str(users["org_b"]), "p": "connx-imp-%"}).fetchone()
        assert n[0] == 0


def test_org_b_cannot_see_imported_source(client, users, ext_pg):
    """داده import شده org A برای org B نامرئی است (GET → 404، فایل نیز)."""
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-iso")
    r = _import(client, users, conn["id"], name="connx-imp-iso-src")
    assert r.status_code == 201
    ds_id = r.json()["data_source_id"]
    tok_b = _token_for(users["b_owner"], users["org_b"])
    gr = client.get(f"/data-sources/{ds_id}", headers=_hdr(tok_b, users["org_b"]))
    assert gr.status_code == 404
    mr = client.post(f"/data-sources/{ds_id}/map", json={"measure_column": "amount"},
                     headers=_hdr(tok_b, users["org_b"]))
    assert mr.status_code == 404


# ---------- ۳) امنیت ----------

def test_import_response_never_contains_secret(client, users, ext_pg):
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-sec")
    r = _import(client, users, conn["id"], name="connx-imp-sec-src")
    assert r.status_code == 201
    assert EXT_SECRET_PLAINTEXT not in r.text
    # پاسخ اتصال هم رمز ندارد
    tok_admin = _token_for(users["admin"], users["org_a"])
    gr = client.get(f"/database-connections/{conn['id']}", headers=_hdr(tok_admin, users["org_a"]))
    assert gr.status_code == 200
    assert EXT_SECRET_PLAINTEXT not in gr.text


def test_import_rejects_sql_like_table_names(client, users, ext_pg):
    """تلاش برای تزریق SQL/ شناسه ناامن → 502 sanitized («table not found»)، نه اجرا."""
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-inj")
    for malicious in ("connx_imp_data; DROP TABLE users", "connx_imp_data--x",
                      "public.connx_imp_data WHERE 1=1"):
        r = _import(client, users, conn["id"], table=malicious)
        assert r.status_code == 502, f"{malicious!r} → {r.status_code}"
        assert "table not found" in r.json()["detail"]
        # جدول واقعی دست‌نخورده (سقف ذخیره‌سازی: هیچ DataSourceای ساخته نشده)
    with seed_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM data_sources WHERE name LIKE :p"),
                         {"p": "connx-imp-inj%"}).fetchone()[0] == 0
        n = c.execute(text("SELECT count(*) FROM information_schema.tables "
                           "WHERE table_schema='public' AND table_name='users'")).fetchone()
        assert n[0] == 1


# ---------- ۴) رفتار خطا ----------

def test_import_disabled_connection_returns_400(client, users, ext_pg):
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-disabled", enabled=False)
    r = _import(client, users, conn["id"])
    assert r.status_code == 400
    assert "غیرفعال" in r.json()["detail"]


def test_import_invalid_credentials_returns_502_sanitized(client, users, ext_pg):
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-badpw",
                                password="wrong-password-dev-only")
    r = _import(client, users, conn["id"], name="connx-imp-badpw-src")
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "connection failed" in detail
    # رمز درست هرگز در خطا لو نمی‌رود
    assert CONNX_READER_PASSWORD not in detail


def test_import_unknown_table_returns_502(client, users, ext_pg):
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-unknown")
    r = _import(client, users, conn["id"], table="no_such_table_xyz")
    assert r.status_code == 502
    assert "table not found" in r.json()["detail"]


def test_import_empty_table_returns_400(client, users, ext_pg):
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-empty")
    r = _import(client, users, conn["id"], table="connx_imp_empty", name="connx-imp-empty-src")
    assert r.status_code == 400
    assert "ردیفی" in r.json()["detail"]
    # هیچ DataSourceای ساخته نشده
    with seed_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM data_sources WHERE name LIKE :p"),
                         {"p": "connx-imp-empty-src%"}).fetchone()[0] == 0


def test_import_connection_failure_returns_502(client, users, ext_pg):
    """اتصال به host غیرقابل‌دسترس → 502 با connect_timeout محدود (نه hang).
    پیام sanitized: ConnectorTimeout → «connection timed out» (بدون DSN/رمز).
    """
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-unreach",
                                host="10.255.255.1")
    r = _import(client, users, conn["id"], name="connx-imp-unreach-src")
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "timed out" in detail or "connection failed" in detail
    assert CONNX_READER_PASSWORD not in detail


def test_import_malformed_request_returns_422(client, users, ext_pg):
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-malformed")
    tok_admin = _token_for(users["admin"], users["org_a"])
    # بدون table
    r1 = client.post(f"/database-connections/{conn['id']}/import", json={"limit": 10},
                     headers=_hdr(tok_admin, users["org_a"]))
    assert r1.status_code == 422
    # table خالی
    r2 = client.post(f"/database-connections/{conn['id']}/import", json={"table": ""},
                     headers=_hdr(tok_admin, users["org_a"]))
    assert r2.status_code == 422
    # limit غیرعددی
    r3 = client.post(f"/database-connections/{conn['id']}/import", json={"table": "connx_imp_data", "limit": "abc"},
                     headers=_hdr(tok_admin, users["org_a"]))
    assert r3.status_code == 422
    # هیچ DataSourceای ساخته نشده
    with seed_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM data_sources WHERE name LIKE :p"),
                         {"p": "connx-imp-malformed%"}).fetchone()[0] == 0


def test_import_requires_admin_role(client, users, ext_pg):
    """import مثل create/delete/test فقط admin+ (§6 ماتریس؛ §7 می‌گوید admin+)."""
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-role")
    tok_manager = _token_for(users["manager"], users["org_a"])
    r = _import(client, users, conn["id"], role="manager")
    assert r.status_code == 403
    # manager حتی با direct body هم رد می‌شود
    r2 = client.post(f"/database-connections/{conn['id']}/import", json={"table": "connx_imp_data"},
                     headers=_hdr(tok_manager, users["org_a"]))
    assert r2.status_code == 403


# ---------- ۵) Provenance: حذف اتصال ----------

def test_delete_connection_sets_null_and_data_survives(client, users, ext_pg):
    conn = _create_conn_via_api(client, users, ext_pg, name="connx-imp-del")
    r = _import(client, users, conn["id"], name="connx-imp-del-src")
    assert r.status_code == 201
    ds_id = uuid.UUID(r.json()["data_source_id"])
    tok_admin = _token_for(users["admin"], users["org_a"])

    # map قبل از حذف اتصال
    mr = client.post(f"/data-sources/{ds_id}/map", json={"measure_column": "amount"},
                     headers=_hdr(tok_admin, users["org_a"]))
    assert mr.status_code == 200 and mr.json()["inserted_rows"] == 3

    # حذف اتصال (admin+)
    dr = client.delete(f"/database-connections/{conn['id']}", headers=_hdr(tok_admin, users["org_a"]))
    assert dr.status_code == 204

    with seed_engine.connect() as c:
        row = c.execute(text(
            "SELECT database_connection_id, status FROM data_sources WHERE id = :i"),
            {"i": str(ds_id)}).fetchone()
        assert row.database_connection_id is None  # ON DELETE SET NULL
        assert row.status == "mapped"
        n = c.execute(text("SELECT count(*) FROM fact_rows WHERE data_source_id = :i"),
                      {"i": str(ds_id)}).fetchone()
        assert n[0] == 3  # داده‌های fact سالم ماندند

    # DataSource هنوز کاملاً usable است (map مجدد)
    mr2 = client.post(f"/data-sources/{ds_id}/map", json={"measure_column": "amount"},
                      headers=_hdr(tok_admin, users["org_a"]))
    assert mr2.status_code == 200
    assert mr2.json()["inserted_rows"] == 3


# ---------- ۶) Migration / schema ----------

def test_data_sources_provenance_column_schema():
    """ستون database_connection_id: nullable، FK به database_connections با ON DELETE SET NULL."""
    with seed_engine.connect() as c:
        col = c.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='data_sources' AND column_name='database_connection_id'")).fetchone()
        assert col is not None and col.is_nullable == "YES"
        fk = c.execute(text(
            "SELECT rc.delete_rule, ccu.table_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name "
            "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name "
            "JOIN information_schema.referential_constraints rc ON rc.constraint_name = tc.constraint_name "
            "WHERE tc.table_name='data_sources' AND tc.constraint_type='FOREIGN KEY' "
            "AND kcu.column_name='database_connection_id'")).fetchone()
        assert fk is not None
        assert fk.table_name == "database_connections"
        assert fk.delete_rule == "SET NULL"
        # index روی ستون (الگوی ریپو برای ستون‌های org/ارجاعی)
        idx = c.execute(text(
            "SELECT 1 FROM pg_indexes WHERE tablename='data_sources' "
            "AND indexdef LIKE '%database_connection_id%'")).fetchone()
        assert idx is not None


def test_null_provenance_upload_still_works(client, users, ext_pg):
    """آپلود CSV عادی همچنان کار می‌کند و database_connection_id آن NULL است — بدون رگرسیون."""
    tok_admin = _token_for(users["admin"], users["org_a"])
    csv_bytes = b"amount,label\n5.5,foo\n7.5,bar\n"
    up = client.post("/data-sources/upload", files={"file": ("connx-imp-upload.csv", io.BytesIO(csv_bytes), "text/csv")},
                     headers=_hdr(tok_admin, users["org_a"]))
    assert up.status_code == 200, up.text
    ds_id = up.json()["id"]
    gr = client.get(f"/data-sources/{ds_id}", headers=_hdr(tok_admin, users["org_a"]))
    assert gr.status_code == 200
    assert gr.json()["database_connection_id"] is None
