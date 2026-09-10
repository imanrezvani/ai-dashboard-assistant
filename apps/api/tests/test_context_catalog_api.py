"""فاز ۲.۴ گام ۲ — تست‌های API اندپوینت GET /context/catalog روی PostgreSQL واقعی

پوشش (docs/PHASE4_CONTEXT_CATALOG_PLAN.md §9/§10.2–10.3):
  - ماتریس نقش‌ها: analyst+ → 200؛ viewer → 403؛ unauthenticated → 401/403؛
    بدون هدر سازمان → 400
  - ایزولاسیون tenant (تست بحرانی): دو org با منابع/KPI هم‌نام — کاتالوگ org A
    صفر identifier از org B دارد (id منبع، id KPI) و برعکس — RLS + فیلتر صریح
  - determinism HTTP: دو فراخوانی متوالی با داده ثابت → payload یکسان بجز generated_at
  - پوشش end-to-end: متادیتای ستون‌ها با mapped_role، تجمیع‌های coverage با مقادیر
    دستی-محاسبه‌شده روی seed واقعی، بلوک KPI با تعریف ذخیره‌شده
  - lifecycle: ساخت/حذف KPI از API → در کاتالوگ می‌آید/می‌رود؛ حذف منبع → می‌رود؛
    حذف اتصال → database_connection_id در کاتالوگ NULL (SET NULL موجود)
  - secret non-disclosure: با ردیف DatabaseConnection واقعی (رمز encrypt شده)،
    host/username/database_name/رمز plaintext هرگز در payload نیستند
  - org بدون داده → 200 با کاتالوگ معتبر خالی (قرارداد empty فاز ۲.۳ §3.3)

ایزولیشن (درس سه گیت قبلی): seed در fixture (زمان اجرا)، override فقط داخل fixture
(بدون دستکاری import-time)، cleanup فقط ردیف‌های خود ماژول (پیشوندهای ctx-api- / ctx-*-catalog).
"""

import os
import pathlib
import sys
import uuid

API_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

TASMIM_APP_PASSWORD = os.getenv("TASMIM_APP_DB_PASSWORD", "tasmim_app_secret_dev_only")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.credentials import encrypt_secret
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
    if env_test:
        if "tasmim_app" in env_test:
            yield env_test
        try:
            yield _make_url(env_test, "tasmim_app", TASMIM_APP_PASSWORD)
        except Exception:
            pass
        yield env_test
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

# RLS جداولی که builder می‌خواند — همان DDL اپ (app/main.py)؛ idempotent
with engine.begin() as conn:
    for tbl in ("data_sources", "data_source_columns", "fact_rows", "kpi_definitions", "database_connections"):
        conn.execute(text(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY"))
        conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {tbl}"))
        conn.execute(text(
            f"CREATE POLICY tenant_isolation ON {tbl} FOR ALL "
            "USING (organization_id::text = current_setting('app.current_org_id', true)) "
            "WITH CHECK (organization_id::text = current_setting('app.current_org_id', true))"
        ))

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


@pytest.fixture(scope="module", autouse=True)
def _api_client():
    """نصب override روی engine تست این ماژول + TestClient؛ پس از ماژول restore می‌شود."""
    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    prev_override = app.dependency_overrides.get(get_db)  # capture داخل fixture — نه import-time
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

ORG_A_SLUG, ORG_B_SLUG, ORG_C_SLUG = "ctx-a-catalog", "ctx-b-catalog", "ctx-c-catalog"
USER_A_EMAIL, USER_B_EMAIL, USER_C_EMAIL = "alice-ctx@org-a.test", "bob-ctx@org-b.test", "carol-ctx@org-c.test"
_MODULE_EMAILS = [
    USER_A_EMAIL, USER_B_EMAIL, USER_C_EMAIL,
    "admin-ctx@org-a.test", "mgr-ctx@org-a.test",
    "ana-ctx@org-a.test", "view-ctx@org-a.test",
]
_SRC_PREFIX = "ctx-api-%"


def _cleanup_own_rows():
    if seed_engine is None:
        return
    with seed_engine.begin() as conn:
        slugs = {"a": ORG_A_SLUG, "b": ORG_B_SLUG, "c": ORG_C_SLUG}
        conn.execute(text(
            "DELETE FROM kpi_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE slug = ANY(:slugs))"), {"slugs": list(slugs.values())})
        conn.execute(text(
            "DELETE FROM fact_rows WHERE data_source_id IN "
            "(SELECT id FROM data_sources WHERE name LIKE :p)"), {"p": _SRC_PREFIX})
        conn.execute(text(
            "DELETE FROM data_source_columns WHERE data_source_id IN "
            "(SELECT id FROM data_sources WHERE name LIKE :p)"), {"p": _SRC_PREFIX})
        conn.execute(text(
            "DELETE FROM data_source_files WHERE data_source_id IN "
            "(SELECT id FROM data_sources WHERE name LIKE :p)"), {"p": _SRC_PREFIX})
        conn.execute(text(
            "DELETE FROM database_connections WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE slug = ANY(:slugs))"), {"slugs": list(slugs.values())})
        conn.execute(text(
            "DELETE FROM data_sources WHERE name LIKE :p OR organization_id IN "
            "(SELECT id FROM organizations WHERE slug = ANY(:slugs))"), {"p": _SRC_PREFIX, "slugs": list(slugs.values())})
        conn.execute(text(
            "DELETE FROM memberships WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE slug = ANY(:slugs))"), {"slugs": list(slugs.values())})
        conn.execute(text(
            "DELETE FROM organizations WHERE slug = ANY(:slugs)"), {"slugs": list(slugs.values())})
        conn.execute(text("DELETE FROM users WHERE email = ANY(:emails)"), {"emails": _MODULE_EMAILS})


@pytest.fixture(scope="module")
def users():
    """org A: owner+admin+manager+analyst+viewer؛ org B: فقط owner؛ org C: فقط owner (بدون داده)."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    _cleanup_own_rows()
    ids = {
        "org_a": uuid.uuid4(), "org_b": uuid.uuid4(), "org_c": uuid.uuid4(),
        "owner": uuid.uuid4(), "admin": uuid.uuid4(),
        "manager": uuid.uuid4(), "analyst": uuid.uuid4(),
        "viewer": uuid.uuid4(), "b_owner": uuid.uuid4(), "c_owner": uuid.uuid4(),
    }
    emails = {
        "owner": USER_A_EMAIL, "admin": "admin-ctx@org-a.test",
        "manager": "mgr-ctx@org-a.test", "analyst": "ana-ctx@org-a.test",
        "viewer": "view-ctx@org-a.test", "b_owner": USER_B_EMAIL, "c_owner": USER_C_EMAIL,
    }
    with seed_engine.begin() as conn:
        for key in emails:
            conn.execute(text(
                "INSERT INTO users (id, email, full_name, hashed_password) VALUES (:i,:e,:f,:h)"),
                {"i": str(ids[key]), "e": emails[key], "f": "U", "h": hash_password("TestPass123")})
        for key, name, slug in (("org_a", "Org A Catalog", ORG_A_SLUG),
                                ("org_b", "Org B Catalog", ORG_B_SLUG),
                                ("org_c", "Org C Catalog", ORG_C_SLUG)):
            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                         {"i": str(ids[key]), "n": name, "s": slug})
        for key, role in (("owner", "owner"), ("admin", "admin"), ("manager", "manager"),
                          ("analyst", "analyst"), ("viewer", "viewer")):
            conn.execute(text(
                "INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:i,:u,:o,:r)"),
                {"i": str(uuid.uuid4()), "u": str(ids[key]), "o": str(ids["org_a"]), "r": role})
        for key, org in (("b_owner", "org_b"), ("c_owner", "org_c")):
            conn.execute(text(
                "INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:i,:u,:o,'owner')"),
                {"i": str(uuid.uuid4()), "u": str(ids[key]), "o": str(ids[org])})
    yield ids
    _cleanup_own_rows()


@pytest.fixture(scope="module")
def data(users):
    """org A: اتصال واقعی + منبع mapped با ۳ fact + منبع import شده pending + KPI؛
    org B: mirror هم‌نام (منبع/KPI با همان نام‌ها اما id های متفاوت) — برای تست ایزولاسیون."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    org_a, org_b = users["org_a"], users["org_b"]
    ds_id, ds_pending_id, conn_row_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with seed_engine.begin() as conn:
        # اتصال با اعتبارنامه واقعی (ciphertext) — برای تست non-disclosure + provenance
        conn.execute(text(
            "INSERT INTO database_connections (id, organization_id, name, engine, host, port, "
            "database_name, username, encrypted_password, enabled, status) "
            "VALUES (:i,:o,'ctx-api-conn','postgresql','ctx-secret-host.internal',5432,"
            "'ctx-secret-dbname','ctx-secret-user',:ep,true,'unknown')"),
            {"i": str(conn_row_id), "o": str(org_a), "ep": encrypt_secret("ctx-secret-password")})
        # منبع mapped org A + متادیتای ستون‌ها (mapped_role) + ۳ fact
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status, database_connection_id) "
            "VALUES (:i,:o,'ctx-api-sales','csv',3,'mapped',:c)"),
            {"i": str(ds_id), "o": str(org_a), "c": str(conn_row_id)})
        for name, pos, dtype, role in (("day", 0, "object", "date"),
                                       ("segment", 1, "object", "category"),
                                       ("amount", 2, "float64", "measure")):
            conn.execute(text(
                "INSERT INTO data_source_columns (id, organization_id, data_source_id, name, position, dtype, mapped_role) "
                "VALUES (:i,:o,:d,:n,:p,:t,:r)"),
                {"i": str(uuid.uuid4()), "o": str(org_a), "d": str(ds_id), "n": name, "p": pos, "t": dtype, "r": role})
        conn.execute(text(
            "INSERT INTO fact_rows (id, organization_id, data_source_id, measure_value, "
            "dimension_date, dimension_category, dimension_label) VALUES "
            "(:i1,:o,:d,10.5,'2026-01-05','alpha','a1'),"
            "(:i2,:o,:d,20.0,'2026-01-12','beta','b1'),"
            "(:i3,:o,:d,30.25,'2026-02-02','alpha','a2')"),
            {"i1": str(uuid.uuid4()), "i2": str(uuid.uuid4()), "i3": str(uuid.uuid4()),
             "o": str(org_a), "d": str(ds_id)})
        # منبع import شده (provenance دارد، بدون fact) — pending
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status, database_connection_id) "
            "VALUES (:i,:o,'ctx-api-imported','postgres',0,'pending',:c)"),
            {"i": str(ds_pending_id), "o": str(org_a), "c": str(conn_row_id)})
        # KPI org A
        conn.execute(text(
            "INSERT INTO kpi_definitions (id, organization_id, data_source_id, name, aggregation, filters, "
            "group_by, granularity, enabled) "
            "VALUES (:i,:o,:d,'ctx-api-rev','sum','[]'::jsonb,'date','month',true)"),
            {"i": str(uuid.uuid4()), "o": str(org_a), "d": str(ds_id)})
        # mirror هم‌نام org B — منبع/KPI با همان نام‌ها، id متفاوت
        ds_b_id = uuid.uuid4()
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status) "
            "VALUES (:i,:o,'ctx-api-sales','csv',1,'mapped')"),
            {"i": str(ds_b_id), "o": str(org_b)})
        conn.execute(text(
            "INSERT INTO fact_rows (id, organization_id, data_source_id, measure_value, "
            "dimension_date, dimension_category, dimension_label) VALUES "
            "(:i1,:o,:d,5.0,'2026-03-01','alpha','a1')"),
            {"i1": str(uuid.uuid4()), "o": str(org_b), "d": str(ds_b_id)})
        conn.execute(text(
            "INSERT INTO kpi_definitions (id, organization_id, data_source_id, name, aggregation, filters, "
            "group_by, granularity, enabled) "
            "VALUES (:i,:o,:d,'ctx-api-rev','sum','[]'::jsonb,'date','month',true)"),
            {"i": str(uuid.uuid4()), "o": str(org_b), "d": str(ds_b_id)})
    yield {
        "org_a": org_a, "org_b": org_b, "org_c": users["org_c"],
        "ds_id": ds_id, "ds_pending_id": ds_pending_id,
        "connection_id": conn_row_id,
    }


@pytest.fixture()
def client(_api_client):
    return _api_client


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _token_for(user_id, org_id):
    return create_access_token({"sub": str(user_id), "org_id": str(org_id)})


def _catalog(client, users, key="analyst", org="org_a"):
    return client.get("/context/catalog", headers=_hdr(_token_for(users[key], users[org]), users[org]))


# ---------- auth matrix ----------

def test_auth_matrix(client, users):
    # unauthenticated → 401
    assert client.get("/context/catalog", headers={"X-Organization-Id": str(users["org_a"])}).status_code in (401, 403)
    # بدون هدر سازمان و بدون org_id در JWT → 400 (قرارداد require_role موجود؛
    # توکن دارای org_id در claim خودش به‌عنوان fallback معتبر است)
    token_no_org = create_access_token({"sub": str(users["analyst"])})
    r = client.get("/context/catalog", headers={"Authorization": f"Bearer {token_no_org}"})
    assert r.status_code == 400
    # viewer → 403
    assert client.get("/context/catalog", headers=_hdr(_token_for(users["viewer"], users["org_a"]), users["org_a"])).status_code == 403
    # analyst/manager/owner → 200
    for key in ("analyst", "manager", "owner"):
        r = client.get("/context/catalog", headers=_hdr(_token_for(users[key], users["org_a"]), users["org_a"]))
        assert r.status_code == 200, f"{key}: {r.text}"


def test_empty_org_returns_valid_empty_catalog_http(client, users):
    r = _catalog(client, users, key="c_owner", org="org_c")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == 1
    assert body["summary"]["data_source_count"] == 0
    assert body["summary"]["kpi_count"] == 0
    assert body["summary"]["date_span"]["min"] is None
    assert body["data_sources"] == []
    assert body["kpis"] == []


# ---------- tenant isolation (critical) ----------

def test_no_cross_org_identifier_leak_either_direction(client, users, data):
    a = _catalog(client, users, key="analyst", org="org_a").json()
    b = _catalog(client, users, key="b_owner", org="org_b").json()

    a_blob = str(a)
    b_blob = str(b)

    # org A هیچ identifier ای از org B ندارد (id منبع، id KPI، نام org، slug)
    assert str(data["org_b"]) not in a_blob
    assert ORG_B_SLUG not in a_blob
    # org B هیچ identifier ای از org A ندارد — حتی با منابع/KPI هم‌نام
    assert str(data["org_a"]) not in b_blob
    assert ORG_A_SLUG not in b_blob
    assert str(data["connection_id"]) not in b_blob

    # هر دو org دقیقاً داده خودشان را می‌بینند (نه هم‌نامِ دیگری)
    assert a["summary"]["data_source_count"] == 2
    assert a["summary"]["kpi_count"] == 1
    assert b["summary"]["data_source_count"] == 1
    assert b["summary"]["kpi_count"] == 1
    a_source_ids = {s["id"] for s in a["data_sources"]}
    assert str(data["ds_id"]) in a_source_ids
    assert str(data["ds_pending_id"]) in a_source_ids


# ---------- determinism ----------

def test_two_consecutive_calls_identical_except_generated_at(client, users, data):
    a1 = _catalog(client, users, key="analyst", org="org_a").json()
    a2 = _catalog(client, users, key="analyst", org="org_a").json()
    g1, g2 = a1.pop("generated_at"), a2.pop("generated_at")
    assert g1.endswith("Z") and g2.endswith("Z")
    assert a1 == a2


# ---------- end-to-end coverage ----------

def test_catalog_content_matches_seeded_facts(client, users, data):
    body = _catalog(client, users, key="analyst", org="org_a").json()

    assert body["organization_id"] == str(users["org_a"])
    # KPI block — تعریف ذخیره‌شده، بدون هیچ مقدار محاسبه‌شده
    kpi = body["kpis"][0]
    assert kpi["name"] == "ctx-api-rev"
    assert kpi["aggregation"] == "sum" and kpi["group_by"] == "date" and kpi["granularity"] == "month"
    assert kpi["data_source_id"] == str(data["ds_id"])
    assert kpi["enabled"] is True
    assert "value" not in kpi and "buckets" not in kpi

    src = next(s for s in body["data_sources"] if s["id"] == str(data["ds_id"]))
    # ستون‌ها با position و mapped_role
    assert [(c["name"], c["mapped_role"]) for c in src["columns"]] == [
        ("day", "date"), ("segment", "category"), ("amount", "measure"),
    ]
    # coverage — مقادیر دستی-محاسبه‌شده روی seed واقعی
    cov = src["coverage"]
    assert cov["fact_row_count"] == 3
    assert cov["date_min"] == "2026-01-05"
    assert cov["date_max"] == "2026-02-02"
    assert cov["has_date_dimension"] is True
    assert cov["category_values"] == ["alpha", "beta"]
    assert cov["category_value_count"] == 2
    assert cov["label_values"] == ["a1", "a2", "b1"]
    assert cov["label_value_count"] == 3
    assert cov["last_mapped_at"] is not None
    # provenance اتصال روی منبع mapped
    assert src["database_connection_id"] == str(data["connection_id"])

    # منبع import شده pending: coverage صفر، provenance دارد
    pending = next(s for s in body["data_sources"] if s["id"] == str(data["ds_pending_id"]))
    assert pending["status"] == "pending" and pending["file_type"] == "postgres"
    assert pending["coverage"]["fact_row_count"] == 0
    assert pending["coverage"]["date_min"] is None
    assert pending["database_connection_id"] == str(data["connection_id"])

    # date_span سراسری از دو منبع A (فقط منبع mapped تاریخ دارد)
    assert body["summary"]["date_span"]["min"] == "2026-01-05"
    assert body["summary"]["date_span"]["max"] == "2026-02-02"


# ---------- lifecycle ----------

def test_kpi_create_and_delete_reflected_in_catalog(client, users, data):
    hdr = _hdr(_token_for(users["manager"], users["org_a"]), users["org_a"])
    payload = {
        "name": "ctx-api-lifecycle",
        "data_source_id": str(data["ds_id"]),
        "aggregation": "avg",
        "filters": [],
        "group_by": None,
        "granularity": None,
    }
    r = client.post("/kpis", json=payload, headers=hdr)
    assert r.status_code == 201, r.text
    kpi_id = r.json()["id"]

    names = [k["name"] for k in _catalog(client, users, key="analyst", org="org_a").json()["kpis"]]
    assert "ctx-api-lifecycle" in names

    assert client.delete(f"/kpis/{kpi_id}", headers=hdr).status_code == 204
    names = [k["name"] for k in _catalog(client, users, key="analyst", org="org_a").json()["kpis"]]
    assert "ctx-api-lifecycle" not in names
    assert _catalog(client, users, key="analyst", org="org_a").json()["summary"]["kpi_count"] == 1


def test_source_delete_reflected_in_catalog(client, users, data):
    # منبع موقت با یک fact + KPI — حذف با superuser (شبیه‌سازی cascade موجود: KPI/fact هم می‌روند)
    tmp_id = uuid.uuid4()
    with seed_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status) "
            "VALUES (:i,:o,'ctx-api-tmp','csv',0,'mapped')"),
            {"i": str(tmp_id), "o": str(users["org_a"])})
        conn.execute(text(
            "INSERT INTO fact_rows (id, organization_id, data_source_id, measure_value) VALUES (:i,:o,:d,1.0)"),
            {"i": str(uuid.uuid4()), "o": str(users["org_a"]), "d": str(tmp_id)})
        conn.execute(text(
            "INSERT INTO kpi_definitions (id, organization_id, data_source_id, name, aggregation, filters, enabled) "
            "VALUES (:i,:o,:d,'ctx-api-tmp-kpi','sum','[]'::jsonb,true)"),
            {"i": str(uuid.uuid4()), "o": str(users["org_a"]), "d": str(tmp_id)})

    body = _catalog(client, users, key="analyst", org="org_a").json()
    assert body["summary"]["data_source_count"] == 3
    assert body["summary"]["kpi_count"] == 2

    with seed_engine.begin() as conn:
        conn.execute(text("DELETE FROM data_sources WHERE id = :i"), {"i": str(tmp_id)})

    body = _catalog(client, users, key="analyst", org="org_a").json()
    assert body["summary"]["data_source_count"] == 2
    assert body["summary"]["kpi_count"] == 1  # KPI cascade شده — بدون ردیف یتیم
    assert all(s["id"] != str(tmp_id) for s in body["data_sources"])


def test_connection_delete_nulls_provenance_in_catalog(client, users, data):
    with seed_engine.begin() as conn:
        conn.execute(text("DELETE FROM database_connections WHERE id = :i"), {"i": str(data["connection_id"])})

    body = _catalog(client, users, key="analyst", org="org_a").json()
    # SET NULL موجود: منابع دست‌نخورده می‌مانند، فقط خط منشأ NULL می‌شود
    for s in body["data_sources"]:
        assert s["database_connection_id"] is None


# ---------- secret non-disclosure ----------

def test_no_connection_secrets_in_payload(client, users, data):
    r = _catalog(client, users, key="analyst", org="org_a")
    assert r.status_code == 200
    blob = r.text
    for forbidden in ("ctx-secret-host.internal", "ctx-secret-user", "ctx-secret-dbname",
                      "ctx-secret-password", "encrypted_password", "ssl_mode"):
        assert forbidden not in blob, f"secret material leaked: {forbidden}"
