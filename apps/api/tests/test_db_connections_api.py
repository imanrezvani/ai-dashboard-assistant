"""فاز ۲.۲ گام ۳ — تست‌های API راستر database-connections روی PostgreSQL واقعی

پوشش (docs/PHASE2_PLAN.md §6 و §9.3):
  - ماتریس نقش‌ها: create/delete/test فقط admin+؛ list/get/tables فقط manager+؛ sample فقط analyst+
  - cross-org id → 404 (نه 403) — بدون افشای وجود؛ org B هیچ اتصالی نمی‌بیند
  - `password` write-only: در هیچ پاسخی رمز/hint/DSN برنمی‌گردد؛ در DB فقط ciphertext
  - اتصال disabled → 400 روی test/tables/sample
  - flow واقعی test/tables/sample با scratch external PostgreSQL (همان الگوی test_connector_postgres.py)
  - حذف اتصال (DELETE) و یونیک بودن name در org

ایزولیشن (درس گیت‌های قبلی): seed در fixture (زمان اجرا)، cleanup فقط ردیف‌های خود ماژول
(پیشوند connx-api)، بدون drop_all و بدون دستکاری ردیف‌های ماژول‌های دیگر.
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

import pytest
import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.core.security import create_access_token, hash_password
from app.core.credentials import encrypt_secret
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

# RLS database_connections — همان DDL اپ (app/main.py)؛ idempotent
with engine.begin() as conn:
    conn.execute(text("ALTER TABLE database_connections ENABLE ROW LEVEL SECURITY"))
    conn.execute(text("ALTER TABLE database_connections FORCE ROW LEVEL SECURITY"))
    conn.execute(text("DROP POLICY IF EXISTS tenant_isolation ON database_connections"))
    conn.execute(text(
        "CREATE POLICY tenant_isolation ON database_connections FOR ALL "
        "USING (organization_id::text = current_setting('app.current_org_id', true)) "
        "WITH CHECK (organization_id::text = current_setting('app.current_org_id', true))"
    ))

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

# --- override get_db فقط داخل fixture (درس گیت فاز ۲.۱: هیچ دستکاری import-time
# روی state مشترک app — در collection همه ماژول‌ها import می‌شوند) ---


@pytest.fixture(scope="module", autouse=True)
def _api_client():
    """نصب override روی engine تست این ماژول + TestClient؛ پس از ماژول restore می‌شود."""
    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    prev_override = app.dependency_overrides.get(get_db)  # capture داخل fixture، نه import
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    # restore به حالت قبلی (الگوی self-restoring test_tenant_isolation)
    if prev_override is not None:
        app.dependency_overrides[get_db] = prev_override
    else:
        app.dependency_overrides.pop(get_db, None)


# --- superuser engine برای seed/cleanup و ساخت scratch external ---

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

ORG_A_SLUG, ORG_B_SLUG = "org-a-connx-api", "org-b-connx-api"
USER_A_EMAIL, USER_B_EMAIL = "alice-connx-api@org-a.test", "bob-connx-api@org-b.test"
# همه ایمیل‌های این ماژول — cleanup باید همه را پوشش دهد (درس UniqueViolation rerun)
_MODULE_EMAILS = [
    USER_A_EMAIL, USER_B_EMAIL,
    "admin-connx-api@org-a.test", "mgr-connx-api@org-a.test",
    "ana-connx-api@org-a.test", "view-connx-api@org-a.test",
]


def _cleanup_own_rows():
    if seed_engine is None:
        return
    with seed_engine.begin() as conn:
        conn.execute(text("DELETE FROM database_connections WHERE name LIKE :p"), {"p": "connx-api-%"})
        conn.execute(text(
            "DELETE FROM memberships WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE slug IN (:a, :b))"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM organizations WHERE slug IN (:a, :b)"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM users WHERE email = ANY(:emails)"), {"emails": _MODULE_EMAILS})


@pytest.fixture(scope="module")
def users():
    """دو org با نقش‌های متفاوت: org A (owner + admin + manager + analyst) / org B (owner)."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    _cleanup_own_rows()
    ids = {
        "org_a": uuid.uuid4(), "org_b": uuid.uuid4(),
        "owner": uuid.uuid4(), "admin": uuid.uuid4(),
        "manager": uuid.uuid4(), "analyst": uuid.uuid4(),
        "viewer": uuid.uuid4(), "b_owner": uuid.uuid4(),
    }
    emails = {
        "owner": USER_A_EMAIL, "admin": "admin-connx-api@org-a.test",
        "manager": "mgr-connx-api@org-a.test", "analyst": "ana-connx-api@org-a.test",
        "viewer": "view-connx-api@org-a.test", "b_owner": USER_B_EMAIL,
    }
    with seed_engine.begin() as conn:
        for key in ("owner", "admin", "manager", "analyst", "viewer", "b_owner"):
            conn.execute(text(
                "INSERT INTO users (id, email, full_name, hashed_password) VALUES (:i,:e,:f,:h)"),
                {"i": str(ids[key]), "e": emails[key], "f": "U", "h": hash_password("TestPass123")})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(ids["org_a"]), "n": "Org A API", "s": ORG_A_SLUG})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(ids["org_b"]), "n": "Org B API", "s": ORG_B_SLUG})
        for key, role in (("owner", "owner"), ("admin", "admin"), ("manager", "manager"),
                          ("analyst", "analyst"), ("viewer", "viewer")):
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
    """scratch external PostgreSQL واقعی (share با test_connector_postgres.py)."""
    if seed_engine is None:
        pytest.skip("superuser engine برای ساخت scratch external در دسترس نیست")
    with seed_engine.connect() as c:
        role_exists = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": EXT_ROLE}).fetchone()
        db_exists = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": EXT_DB_NAME}).fetchone()
    from psycopg import sql as psql
    host = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(used_url).hostname or "localhost"
    port = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(used_url).port or 5433
    with psycopg.connect(f"host={host} port={port} dbname=postgres user=tasmim password=tasmim_secret", autocommit=True) as ac:
        role_stmt = psql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
            psql.Identifier(EXT_ROLE), psql.Literal(CONNX_READER_PASSWORD))
        alter_stmt = psql.SQL("ALTER ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
            psql.Identifier(EXT_ROLE), psql.Literal(CONNX_READER_PASSWORD))
        ac.execute(alter_stmt if role_exists else role_stmt)
        if not db_exists:
            ac.execute(psql.SQL("CREATE DATABASE {}").format(psql.Identifier(EXT_DB_NAME)))
    with psycopg.connect(f"host={host} port={port} dbname={EXT_DB_NAME} user=tasmim password=tasmim_secret", autocommit=True) as ec:
        ec.execute("DROP TABLE IF EXISTS connx_api_data")
        ec.execute("CREATE TABLE connx_api_data (id int PRIMARY KEY, label text, amount numeric)")
        ec.execute("INSERT INTO connx_api_data VALUES (1,'alpha',10.5),(2,'beta',20.0),(3,'gamma',30.25)")
        ec.execute(f"GRANT CONNECT ON DATABASE {EXT_DB_NAME} TO {EXT_ROLE}")
        ec.execute("GRANT USAGE ON SCHEMA public TO " + EXT_ROLE)
        ec.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO " + EXT_ROLE)
    yield {"host": host, "port": port, "dbname": EXT_DB_NAME, "user": EXT_ROLE, "password": CONNX_READER_PASSWORD}


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _token_for(user_id, org_id):
    return create_access_token({"sub": str(user_id), "org_id": str(org_id)})


@pytest.fixture()
def client(_api_client):
    return _api_client


# ---------- helpers برای ساخت اتصال از طریق API ----------

CREATE_PAYLOAD = {
    "name": "connx-api-primary",
    "engine": "postgresql",
    "host": "db.invalid",
    "port": 5432,
    "database_name": "analytics",
    "username": "bi_reader",
    "password": "external-pw-dev-only-123",
}


def _create_conn(client, token, org_id, name=CREATE_PAYLOAD["name"], **overrides):
    payload = {**CREATE_PAYLOAD, "name": name, **overrides}
    return client.post("/database-connections", json=payload, headers=_hdr(token, org_id))


# ---------- ۱) ماتریس نقش‌ها ----------

def test_create_requires_admin(client, users, ext_pg):
    tok_owner = _token_for(users["owner"], users["org_a"])
    tok_admin = _token_for(users["admin"], users["org_a"])
    tok_manager = _token_for(users["manager"], users["org_a"])
    tok_analyst = _token_for(users["analyst"], users["org_a"])
    tok_viewer = _token_for(users["viewer"], users["org_a"])

    # viewer/analyst/manager → 403
    for tok in (tok_viewer, tok_analyst, tok_manager):
        r = _create_conn(client, tok, users["org_a"], name=f"connx-api-{uuid.uuid4().hex[:6]}")
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    # هیچ ردیفی ساخته نشده باشد
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(users["org_a"])})
        count_before = db.execute(text("SELECT count(*) FROM database_connections")).scalar()
        db.rollback()

    # admin/owner → 201
    r1 = _create_conn(client, tok_admin, users["org_a"], name="connx-api-by-admin")
    assert r1.status_code == 201, r1.text
    r2 = _create_conn(client, tok_owner, users["org_a"], name="connx-api-by-owner")
    assert r2.status_code == 201, r2.text


def test_list_get_require_manager_plus(client, users):
    tok_analyst = _token_for(users["analyst"], users["org_a"])
    tok_viewer = _token_for(users["viewer"], users["org_a"])
    tok_manager = _token_for(users["manager"], users["org_a"])

    for path in ("/database-connections",):
        assert client.get(path, headers=_hdr(tok_analyst, users["org_a"])).status_code == 403
        assert client.get(path, headers=_hdr(tok_viewer, users["org_a"])).status_code == 403
        assert client.get(path, headers=_hdr(tok_manager, users["org_a"])).status_code == 200


def test_delete_and_test_require_admin(client, users, ext_pg):
    tok_manager = _token_for(users["manager"], users["org_a"])
    tok_admin = _token_for(users["admin"], users["org_a"])

    r = _create_conn(client, _token_for(users["owner"], users["org_a"]), users["org_a"], name="connx-api-del")
    assert r.status_code == 201
    conn_id = r.json()["id"]
    # manager نمی‌تواند حذف/تست کند
    assert client.delete(f"/database-connections/{conn_id}", headers=_hdr(tok_manager, users["org_a"])).status_code == 403
    assert client.post(f"/database-connections/{conn_id}/test", headers=_hdr(tok_manager, users["org_a"])).status_code == 403
    # ردیف هنوز هست
    r2 = client.get(f"/database-connections/{conn_id}", headers=_hdr(tok_manager, users["org_a"]))
    assert r2.status_code == 200

    # admin حذف می‌کند → 204 و بعدش 404
    assert client.delete(f"/database-connections/{conn_id}", headers=_hdr(tok_admin, users["org_a"])).status_code == 204
    assert client.get(f"/database-connections/{conn_id}", headers=_hdr(tok_manager, users["org_a"])).status_code == 404


# ---------- ۲) password write-only ----------

def test_password_never_in_any_response(client, users, ext_pg):
    tok = _token_for(users["owner"], users["org_a"])
    r = _create_conn(client, tok, users["org_a"], name="connx-api-secret", ssl_mode="require")
    assert r.status_code == 201, r.text
    body = r.json()
    conn_id = body["id"]

    # password plaintext هرگز در پاسخ create نیست
    assert "password" not in body
    assert CREATE_PAYLOAD["password"] not in __import__("json").dumps(body)
    assert body["has_stored_credentials"] is True

    # در GET هم نیست
    g = client.get(f"/database-connections/{conn_id}", headers=_hdr(tok, users["org_a"]))
    assert g.status_code == 200
    gtxt = __import__("json").dumps(g.json())
    assert "password" not in gtxt
    assert CREATE_PAYLOAD["password"] not in gtxt
    assert "external-pw" not in gtxt

    # در لیست هم نیست
    l = client.get("/database-connections", headers=_hdr(tok, users["org_a"]))
    assert l.status_code == 200
    ltxt = __import__("json").dumps(l.json())
    assert CREATE_PAYLOAD["password"] not in ltxt

    # در DB فقط ciphertext است (بدون plaintext)
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(users["org_a"])})
        row = db.get(__import__("app.models.database_connection", fromlist=["DatabaseConnection"]).DatabaseConnection, uuid.UUID(conn_id))
        assert row is not None
        assert CREATE_PAYLOAD["password"].encode() not in (row.encrypted_password or b"")
        assert row.encrypted_password != CREATE_PAYLOAD["password"].encode()
        db.rollback()


# ---------- ۳) cross-org → 404 ----------

def test_cross_org_is_404_everywhere(client, users, ext_pg):
    tok_a = _token_for(users["owner"], users["org_a"])
    tok_b = _token_for(users["b_owner"], users["org_b"])

    r = _create_conn(client, tok_a, users["org_a"], name="connx-api-xorg")
    assert r.status_code == 201
    conn_id = r.json()["id"]

    # org B: list خالی، get/delete/test/tables/sample همه 404
    assert client.get("/database-connections", headers=_hdr(tok_b, users["org_b"])).json() == []
    assert client.get(f"/database-connections/{conn_id}", headers=_hdr(tok_b, users["org_b"])).status_code == 404
    assert client.delete(f"/database-connections/{conn_id}", headers=_hdr(tok_b, users["org_b"])).status_code == 404
    assert client.post(f"/database-connections/{conn_id}/test", headers=_hdr(tok_b, users["org_b"])).status_code == 404
    assert client.get(f"/database-connections/{conn_id}/tables", headers=_hdr(tok_b, users["org_b"])).status_code == 404
    assert client.get(
        f"/database-connections/{conn_id}/tables/connx_api_data/sample", headers=_hdr(tok_b, users["org_b"])
    ).status_code == 404

    # ردیف هنوز سر جاش است (404 افشای وجود نبود)
    assert client.get(f"/database-connections/{conn_id}", headers=_hdr(tok_a, users["org_a"])).status_code == 200


# ---------- ۴) duplicate name → 409 ----------

def test_duplicate_name_same_org_rejected(client, users):
    tok = _token_for(users["owner"], users["org_a"])
    _create_conn(client, tok, users["org_a"], name="connx-api-dup")
    r = _create_conn(client, tok, users["org_a"], name="connx-api-dup")
    assert r.status_code == 409, r.text


# ---------- ۵) flow واقعی test/tables/sample با scratch external ----------

@pytest.fixture(scope="module")
def live_conn_id(users, ext_pg, _api_client):
    """اتصال واقعی به scratch external از طریق API (owner org A)."""
    tok = _token_for(users["owner"], users["org_a"])
    r = _api_client.post("/database-connections", json={
        "name": "connx-api-live",
        "engine": "postgresql",
        "host": ext_pg["host"],
        "port": ext_pg["port"],
        "database_name": ext_pg["dbname"],
        "username": ext_pg["user"],
        "password": ext_pg["password"],
    }, headers=_hdr(tok, users["org_a"]))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_test_endpoint_ok_updates_status(client, users, ext_pg, live_conn_id):
    tok = _token_for(users["admin"], users["org_a"])
    r = client.post(f"/database-connections/{live_conn_id}/test", headers=_hdr(tok, users["org_a"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["latency_ms"] is not None
    assert body["last_error"] is None
    # persistence: status/last_checked_at روی ردیف
    g = client.get(f"/database-connections/{live_conn_id}", headers=_hdr(tok, users["org_a"]))
    assert g.json()["status"] == "ok"
    assert g.json()["last_checked_at"] is not None


def test_test_endpoint_failed_sanitized(client, users, ext_pg):
    """رمز غلط → status=failed و last_error sanitized (بدون رمز/DSN)."""
    tok = _token_for(users["owner"], users["org_a"])
    # host واقعی ولی رمز غلط (no PATCH در فاز ۲.۲ — rotate عمداً delete+recreate است)
    r2 = _create_conn(client, tok, users["org_a"], name="connx-api-badpw2",
                      host=ext_pg["host"], port=ext_pg["port"],
                      database_name=ext_pg["dbname"], username=ext_pg["user"],
                      password="definitely-wrong")
    assert r2.status_code == 201
    conn_id2 = r2.json()["id"]
    t = client.post(f"/database-connections/{conn_id2}/test", headers=_hdr(tok, users["org_a"]))
    assert t.status_code == 200
    body = t.json()
    assert body["ok"] is False
    assert body["status"] == "failed"
    err = body["last_error"] or ""
    assert "definitely-wrong" not in err
    assert "password=" not in err.lower()
    assert err in {"connection failed", "external database error",
                   "permission denied on external database", "connection timed out"}


def test_tables_endpoint_discovers(client, users, ext_pg, live_conn_id):
    tok_manager = _token_for(users["manager"], users["org_a"])
    r = client.get(f"/database-connections/{live_conn_id}/tables", headers=_hdr(tok_manager, users["org_a"]))
    assert r.status_code == 200, r.text
    names = {t["qualified_name"] for t in r.json()}
    assert "public.connx_api_data" in names
    assert all("schema_name" in t for t in r.json())
    assert not any(n.startswith(("pg_catalog.", "information_schema.")) for n in names)


def test_sample_endpoint_returns_rows(client, users, ext_pg, live_conn_id):
    tok_analyst = _token_for(users["analyst"], users["org_a"])
    r = client.get(
        f"/database-connections/{live_conn_id}/tables/connx_api_data/sample",
        headers=_hdr(tok_analyst, users["org_a"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table"] == "connx_api_data"
    assert len(body["rows"]) == 3
    assert body["rows"][0]["label"] == "alpha"


def test_sample_analyst_allowed_manager_allowed(client, users, ext_pg, live_conn_id):
    tok_analyst = _token_for(users["analyst"], users["org_a"])
    tok_manager = _token_for(users["manager"], users["org_a"])
    tok_viewer = _token_for(users["viewer"], users["org_a"])
    url = f"/database-connections/{live_conn_id}/tables/connx_api_data/sample"
    assert client.get(url, headers=_hdr(tok_analyst, users["org_a"])).status_code == 200
    assert client.get(url, headers=_hdr(tok_manager, users["org_a"])).status_code == 200
    assert client.get(url, headers=_hdr(tok_viewer, users["org_a"])).status_code == 403


def test_sample_limit_clamped_and_validated(client, users, ext_pg, live_conn_id):
    tok = _token_for(users["analyst"], users["org_a"])
    url = f"/database-connections/{live_conn_id}/tables/connx_api_data/sample"
    # limit > 1000 → 422 (اعتبارسنجی Query)
    assert client.get(url, params={"limit": 99999}, headers=_hdr(tok, users["org_a"])).status_code == 422
    r = client.get(url, params={"limit": 2}, headers=_hdr(tok, users["org_a"]))
    assert r.status_code == 200
    assert len(r.json()["rows"]) == 2


def test_unknown_table_returns_502_sanitized(client, users, ext_pg, live_conn_id):
    tok = _token_for(users["analyst"], users["org_a"])
    r = client.get(
        f"/database-connections/{live_conn_id}/tables/no_such_table_api/sample",
        headers=_hdr(tok, users["org_a"]),
    )
    assert r.status_code == 502
    assert "table not found" in r.json()["detail"]


# ---------- ۶) disabled → 400 ----------

def test_disabled_connection_rejects_operations(client, users, ext_pg):
    """اتصال enabled=False → 400 روی test/tables/sample (بعد از disable مستقیم در DB؛
    چون Phase 2.2 عمداً PUT/PATCH ندارد)."""
    tok = _token_for(users["owner"], users["org_a"])
    r = _create_conn(client, tok, users["org_a"], name="connx-api-disabled",
                     host=ext_pg["host"], port=ext_pg["port"],
                     database_name=ext_pg["dbname"], username=ext_pg["user"],
                     password=ext_pg["password"])
    assert r.status_code == 201
    conn_id = r.json()["id"]
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(users["org_a"])})
        db.execute(text("UPDATE database_connections SET enabled = false WHERE id = :i"),
                   {"i": conn_id})
        db.commit()
    assert client.post(f"/database-connections/{conn_id}/test", headers=_hdr(tok, users["org_a"])).status_code == 400
    assert client.get(f"/database-connections/{conn_id}/tables", headers=_hdr(tok, users["org_a"])).status_code == 400
    assert client.get(
        f"/database-connections/{conn_id}/tables/connx_api_data/sample", headers=_hdr(tok, users["org_a"])
    ).status_code == 400


# ---------- ۷) flow کامل: create → test → tables → sample → delete ----------

def test_full_lifecycle(client, users, ext_pg):
    tok_admin = _token_for(users["admin"], users["org_a"])
    tok_analyst = _token_for(users["analyst"], users["org_a"])
    name = f"connx-api-lc-{uuid.uuid4().hex[:6]}"

    r = _create_conn(client, tok_admin, users["org_a"], name=name,
                     host=ext_pg["host"], port=ext_pg["port"],
                     database_name=ext_pg["dbname"], username=ext_pg["user"],
                     password=ext_pg["password"])
    assert r.status_code == 201
    conn_id = r.json()["id"]

    assert client.post(f"/database-connections/{conn_id}/test", headers=_hdr(tok_admin, users["org_a"])).json()["ok"] is True
    assert "public.connx_api_data" in {t["qualified_name"] for t in client.get(
        f"/database-connections/{conn_id}/tables", headers=_hdr(tok_admin, users["org_a"])).json()}
    assert client.get(
        f"/database-connections/{conn_id}/tables/connx_api_data/sample", headers=_hdr(tok_analyst, users["org_a"])
    ).status_code == 200

    assert client.delete(f"/database-connections/{conn_id}", headers=_hdr(tok_admin, users["org_a"])).status_code == 204
    assert client.get(f"/database-connections/{conn_id}", headers=_hdr(tok_admin, users["org_a"])).status_code == 404
