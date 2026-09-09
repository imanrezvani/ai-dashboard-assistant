"""فاز ۲.۳ گام ۳ — تست‌های API راستر /kpis روی PostgreSQL واقعی

پوشش (docs/PHASE3_KPI_PLAN.md §4/§8):
  - ماتریس نقش‌ها: create/patch/delete فقط manager+؛ list/get/compute فقط analyst+؛
    viewer هیچ دسترسی‌ای ندارد
  - CRUD با اعتبارسنجی کامل تعریف (aggregation/group_by/granularity/filters/پنجره تاریخ)
  - DataSource فقط همان org + status == "mapped" (منبع map نشده → 400)
  - compute: عاری از منطق تجمیع در راستر — فقط FactRecord + موتور pure گام ۲
  - خروجی compute: decimal-string ۴رقم (قرارداد گام ۲ — هرگز float)
  - cross-org id → 404 (نه 403)؛ لیست organization-scoped
  - نام تکراری در org → 409؛ PATCH نتیجه کامل را revalidate می‌کند
  - هیچ رمز/credentialای در پاسخ‌ها نیست (این راستر اصلاً credential ندارد — باز هم assert می‌کنیم)

ایزولیشن (درس سه گیت قبلی): seed در fixture (زمان اجرا)، override فقط داخل fixture
(بدون دستکاری import-time)، cleanup فقط ردیف‌های خود ماژول (پیشوندهای kpi-api).
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

import pytest
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

# RLS kpi_definitions — همان DDL اپ (app/main.py)؛ idempotent
with engine.begin() as conn:
    conn.execute(text("ALTER TABLE kpi_definitions ENABLE ROW LEVEL SECURITY"))
    conn.execute(text("ALTER TABLE kpi_definitions FORCE ROW LEVEL SECURITY"))
    conn.execute(text("DROP POLICY IF EXISTS tenant_isolation ON kpi_definitions"))
    conn.execute(text(
        "CREATE POLICY tenant_isolation ON kpi_definitions FOR ALL "
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

ORG_A_SLUG, ORG_B_SLUG = "org-a-kpi-api", "org-b-kpi-api"
USER_A_EMAIL, USER_B_EMAIL = "alice-kpi-api@org-a.test", "bob-kpi-api@org-b.test"
_MODULE_EMAILS = [
    USER_A_EMAIL, USER_B_EMAIL,
    "admin-kpi-api@org-a.test", "mgr-kpi-api@org-a.test",
    "ana-kpi-api@org-a.test", "view-kpi-api@org-a.test",
]


def _cleanup_own_rows():
    if seed_engine is None:
        return
    with seed_engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM kpi_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE slug IN (:a, :b))"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text(
            "DELETE FROM fact_rows WHERE data_source_id IN "
            "(SELECT id FROM data_sources WHERE name LIKE :p)"), {"p": "kpi-api-%"})
        conn.execute(text(
            "DELETE FROM data_source_columns WHERE data_source_id IN "
            "(SELECT id FROM data_sources WHERE name LIKE :p)"), {"p": "kpi-api-%"})
        conn.execute(text("DELETE FROM data_sources WHERE name LIKE :p"), {"p": "kpi-api-%"})
        conn.execute(text(
            "DELETE FROM memberships WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE slug IN (:a, :b))"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM organizations WHERE slug IN (:a, :b)"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM users WHERE email = ANY(:emails)"), {"emails": _MODULE_EMAILS})


@pytest.fixture(scope="module")
def users():
    """org A: owner + admin + manager + analyst + viewer؛ org B: فقط owner."""
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
        "owner": USER_A_EMAIL, "admin": "admin-kpi-api@org-a.test",
        "manager": "mgr-kpi-api@org-a.test", "analyst": "ana-kpi-api@org-a.test",
        "viewer": "view-kpi-api@org-a.test", "b_owner": USER_B_EMAIL,
    }
    with seed_engine.begin() as conn:
        for key in ("owner", "admin", "manager", "analyst", "viewer", "b_owner"):
            conn.execute(text(
                "INSERT INTO users (id, email, full_name, hashed_password) VALUES (:i,:e,:f,:h)"),
                {"i": str(ids[key]), "e": emails[key], "f": "U", "h": hash_password("TestPass123")})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(ids["org_a"]), "n": "Org A KPI", "s": ORG_A_SLUG})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(ids["org_b"]), "n": "Org B KPI", "s": ORG_B_SLUG})
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
def source(users):
    """یک DataSource mapped با fact_rows واقعی (سه ردیف، category/label/تاریخ) — مسیر lifecycle موجود."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    org_a = users["org_a"]
    ds_id = uuid.uuid4()
    with seed_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status) "
            "VALUES (:i,:o,:n,'csv',3,'mapped')"),
            {"i": str(ds_id), "o": str(org_a), "n": "kpi-api-sales"})
        conn.execute(text(
            "INSERT INTO fact_rows (id, organization_id, data_source_id, measure_value, "
            "dimension_date, dimension_category, dimension_label) VALUES "
            "(:i1,:o,:d,10.5,'2026-01-05','alpha','a1'),"
            "(:i2,:o,:d,20.0,'2026-01-12','beta','b1'),"
            "(:i3,:o,:d,30.25,'2026-02-02','alpha','a2')"),
            {"i1": str(uuid.uuid4()), "i2": str(uuid.uuid4()), "i3": str(uuid.uuid4()),
             "o": str(org_a), "d": str(ds_id)})
        # یک DataSource map نشده (pending) برای تست status gate
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status) "
            "VALUES (:i,:o,'kpi-api-pending','csv',0,'pending')"),
            {"i": str(uuid.uuid4()), "o": str(org_a)})
    yield {"org_a": org_a, "ds_id": ds_id, "org_b": users["org_b"]}
    # cleanup در users fixture teardown انجام می‌شود (ترتیب yield برعکس: source اول، users بعد)


@pytest.fixture()
def client(_api_client):
    return _api_client


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _token_for(user_id, org_id):
    return create_access_token({"sub": str(user_id), "org_id": str(org_id)})


def _payload(name="kpi-api-total", ds_id=None, **overrides):
    p = {
        "name": name,
        "data_source_id": str(ds_id),
        "aggregation": "sum",
        "filters": [],
        "group_by": None,
        "granularity": None,
    }
    p.update(overrides)
    return p


def _create_kpi(client, token, org_id, **overrides):
    return client.post("/kpis", json=_payload(**overrides), headers=_hdr(token, org_id))


# ---------- role matrix ----------

def test_create_requires_manager_plus(client, users, source):
    ds = str(source["ds_id"])
    # manager ✓
    r = _create_kpi(client, _token_for(users["manager"], source["org_a"]), source["org_a"], ds_id=ds, name="kpi-api-r-mgr")
    assert r.status_code == 201, r.text
    kpi_id = r.json()["id"]
    # analyst ✗ (403)
    r = _create_kpi(client, _token_for(users["analyst"], source["org_a"]), source["org_a"], ds_id=ds, name="kpi-api-r-ana")
    assert r.status_code == 403
    # viewer ✗ (403) — روی همه اندپوینت‌ها
    vtok = _token_for(users["viewer"], source["org_a"])
    assert client.get("/kpis", headers=_hdr(vtok, source["org_a"])).status_code == 403
    assert client.post("/kpis", json=_payload(ds_id=ds), headers=_hdr(vtok, source["org_a"])).status_code == 403
    assert client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(vtok, source["org_a"])).status_code == 403
    assert client.delete(f"/kpis/{kpi_id}", headers=_hdr(vtok, source["org_a"])).status_code == 403
    # unauthenticated → 401/403
    assert client.get("/kpis", headers={"X-Organization-Id": str(source["org_a"])}).status_code in (401, 403)
    # cleanup کپی manager
    client.delete(f"/kpis/{kpi_id}", headers=_hdr(_token_for(users["manager"], source["org_a"]), source["org_a"]))


def test_compute_requires_analyst_plus(client, users, source):
    ds = str(source["ds_id"])
    r = _create_kpi(client, _token_for(users["manager"], source["org_a"]), source["org_a"], ds_id=ds, name="kpi-api-c-ana")
    kpi_id = r.json()["id"]
    # analyst ✓ (compute analyst+ است)
    r = client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"]))
    assert r.status_code == 200, r.text
    # manager هم analyst+ است → ✓
    r = client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["manager"], source["org_a"]), source["org_a"]))
    assert r.status_code == 200
    # viewer ✗
    assert client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["viewer"], source["org_a"]), source["org_a"])).status_code == 403


# ---------- CRUD + validation ----------

def test_list_is_org_scoped_and_excludes_org_b(client, users, source):
    ds = str(source["ds_id"])
    _create_kpi(client, _token_for(users["manager"], source["org_a"]), source["org_a"], ds_id=ds, name="kpi-api-l1")
    # org B یک KPI با نام یکسان می‌سازد — با منبع خودش؛ چون FK org B منبع ندارد، از منبع A استفاده
    # نمی‌تواند بکند (cross-org → 404)؛ پس فقط list A را چک می‌کنیم
    items = client.get("/kpis", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"])).json()
    assert all(i["organization_id"] == str(source["org_a"]) for i in items)
    assert any(i["name"] == "kpi-api-l1" for i in items)
    # org B لیست خالی می‌بیند
    b_items = client.get("/kpis", headers=_hdr(_token_for(users["b_owner"], source["org_b"]), source["org_b"])).json()
    assert b_items == []


def test_get_cross_org_is_404(client, users, source):
    ds = str(source["ds_id"])
    r = _create_kpi(client, _token_for(users["manager"], source["org_a"]), source["org_a"], ds_id=ds, name="kpi-api-x")
    kpi_id = r.json()["id"]
    r = client.get(f"/kpis/{kpi_id}", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"]))
    assert r.status_code == 200
    # org B همان id → 404 (بدون افشای وجود)
    r = client.get(f"/kpis/{kpi_id}", headers=_hdr(_token_for(users["b_owner"], source["org_b"]), source["org_b"]))
    assert r.status_code == 404
    # compute و delete هم 404
    assert client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["b_owner"], source["org_b"]), source["org_b"])).status_code == 404
    assert client.delete(f"/kpis/{kpi_id}", headers=_hdr(_token_for(users["b_owner"], source["org_b"]), source["org_b"])).status_code == 404


def test_cross_org_data_source_rejected(client, users, source):
    # org B می‌خواهد KPI روی منبع org A بسازد → 404 (منبع دیده نمی‌شود)
    r = client.post("/kpis", json=_payload(name="kpi-api-steal", ds_id=str(source["ds_id"])),
                    headers=_hdr(_token_for(users["b_owner"], source["org_b"]), source["org_b"]))
    assert r.status_code == 404
    # KPI در هیچ orgی ساخته نشده
    items = client.get("/kpis", headers=_hdr(_token_for(users["b_owner"], source["org_b"]), source["org_b"])).json()
    assert items == []


def test_duplicate_name_rejected_409(client, users, source):
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    assert _create_kpi(client, tok, source["org_a"], ds_id=ds, name="kpi-api-dup").status_code == 201
    r = _create_kpi(client, tok, source["org_a"], ds_id=ds, name="kpi-api-dup")
    assert r.status_code == 409


def test_definition_validation_422(client, users, source):
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    # aggregation نامعتبر
    assert client.post("/kpis", json=_payload(name="kpi-api-v1", ds_id=ds, aggregation="median"),
                       headers=_hdr(tok, source["org_a"])).status_code == 422
    # group_by نامعتبر
    assert client.post("/kpis", json=_payload(name="kpi-api-v2", ds_id=ds, group_by="quarter"),
                       headers=_hdr(tok, source["org_a"])).status_code == 422
    # group_by=date بدون granularity
    assert client.post("/kpis", json=_payload(name="kpi-api-v3", ds_id=ds, group_by="date"),
                       headers=_hdr(tok, source["org_a"])).status_code == 422
    # granularity بدون group_by=date
    assert client.post("/kpis", json=_payload(name="kpi-api-v4", ds_id=ds, granularity="month"),
                       headers=_hdr(tok, source["org_a"])).status_code == 422
    # فیلتر نامعتبر (op اشتباه برای field)
    r = client.post("/kpis", json=_payload(name="kpi-api-v5", ds_id=ds, filters=[{"field": "category", "op": "gte", "value": "x"}]),
                    headers=_hdr(tok, source["org_a"]))
    assert r.status_code == 422
    # >10 فیلتر
    many = [{"field": "category", "op": "eq", "value": f"c{i}"} for i in range(11)]
    assert client.post("/kpis", json=_payload(name="kpi-api-v6", ds_id=ds, filters=many),
                       headers=_hdr(tok, source["org_a"])).status_code == 422
    # پنجره تاریخ معکوس
    assert client.post("/kpis", json=_payload(name="kpi-api-v7", ds_id=ds, date_from="2026-03-01", date_to="2026-01-01"),
                       headers=_hdr(tok, source["org_a"])).status_code == 422


def test_patch_revalidates_full_definition(client, users, source):
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    r = _create_kpi(client, tok, source["org_a"], ds_id=ds, name="kpi-api-p1")
    kpi_id = r.json()["id"]
    # گروه‌بندی سالم اضافه کن
    r = client.patch(f"/kpis/{kpi_id}", json={"group_by": "category"}, headers=_hdr(tok, source["org_a"]))
    assert r.status_code == 200, r.text
    assert r.json()["group_by"] == "category"
    # granularity روی group_by=category → قاعده iff نقض می‌شود → 422 (نتیجه کامل revalidate شد)
    r = client.patch(f"/kpis/{kpi_id}", json={"granularity": "month"}, headers=_hdr(tok, source["org_a"]))
    assert r.status_code == 422
    # تغییر به aggregation نامعتبر → 422
    r = client.patch(f"/kpis/{kpi_id}", json={"aggregation": "median"}, headers=_hdr(tok, source["org_a"]))
    assert r.status_code == 422
    # تغییر نام به تکراری → 409
    _create_kpi(client, tok, source["org_a"], ds_id=ds, name="kpi-api-p2")
    r = client.patch(f"/kpis/{kpi_id}", json={"name": "kpi-api-p2"}, headers=_hdr(tok, source["org_a"]))
    assert r.status_code == 409
    # تغییر منبع به منبع org دیگر → 404
    r = client.patch(f"/kpis/{kpi_id}", json={"data_source_id": str(uuid.uuid4())}, headers=_hdr(tok, source["org_a"]))
    assert r.status_code == 404
    # patch موفق: فعال‌سازی/غیرفعال‌سازی
    r = client.patch(f"/kpis/{kpi_id}", json={"enabled": False}, headers=_hdr(tok, source["org_a"]))
    assert r.status_code == 200 and r.json()["enabled"] is False


def test_delete_and_recompute_after(client, users, source):
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    r = _create_kpi(client, tok, source["org_a"], ds_id=ds, name="kpi-api-del")
    kpi_id = r.json()["id"]
    assert client.delete(f"/kpis/{kpi_id}", headers=_hdr(tok, source["org_a"])).status_code == 204
    assert client.get(f"/kpis/{kpi_id}", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"])).status_code == 404


def test_source_must_be_mapped_for_create_and_compute(client, users, source):
    tok = _token_for(users["manager"], source["org_a"])
    # ساخت روی منبع pending → 400
    r = client.post("/kpis", json=_payload(name="kpi-api-pend", ds_id=str(uuid.uuid4())),
                    headers=_hdr(tok, source["org_a"]))
    assert r.status_code == 404  # منبع وجود ندارد → 404
    # منبع pending واقعی: fetch مستقیم id از DB
    with seed_engine.connect() as c:
        pending_id = c.execute(text(
            "SELECT id FROM data_sources WHERE name = 'kpi-api-pending'")).scalar()
    r = client.post("/kpis", json=_payload(name="kpi-api-pend2", ds_id=str(pending_id)),
                    headers=_hdr(tok, source["org_a"]))
    assert r.status_code == 400  # وجود دارد ولی map نشده → 400


# ---------- compute semantics ----------

def test_compute_sum_decimal_string(client, users, source):
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    r = _create_kpi(client, tok, source["org_a"], ds_id=ds, name="kpi-api-s1")
    kpi_id = r.json()["id"]
    r = client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"]))
    assert r.status_code == 200
    body = r.json()
    assert body["value"] == "60.7500"  # 10.5 + 20.0 + 30.25 — decimal-string ۴رقم، نه float
    assert body["rows"] == 3
    assert body["kpi_id"] == kpi_id


def test_compute_avg_and_min_max_count(client, users, source):
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    for agg, expected in (("avg", "20.2500"), ("min", "10.5000"), ("max", "30.2500"), ("count", "3.0000")):
        r = _create_kpi(client, tok, source["org_a"], ds_id=ds, name=f"kpi-api-agg-{agg}", aggregation=agg)
        kpi_id = r.json()["id"]
        r = client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"]))
        assert r.status_code == 200
        assert r.json()["value"] == expected, agg


def test_compute_filtered_kpi(client, users, source):
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    r = _create_kpi(client, tok, source["org_a"], ds_id=ds, name="kpi-api-f1",
                    filters=[{"field": "category", "op": "eq", "value": "alpha"}])
    kpi_id = r.json()["id"]
    r = client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"]))
    assert r.status_code == 200
    assert r.json()["value"] == "40.7500"  # فقط alpha: 10.5 + 30.25
    assert r.json()["rows"] == 2


def test_compute_inclusive_date_window(client, users, source):
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    # پنجره inclusive: [2026-01-05, 2026-01-12] → دو ردیف
    r = _create_kpi(client, tok, source["org_a"], ds_id=ds, name="kpi-api-w1",
                    date_from="2026-01-05", date_to="2026-01-12")
    kpi_id = r.json()["id"]
    r = client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"]))
    assert r.status_code == 200
    assert r.json()["value"] == "30.5000"  # 10.5 + 20.0 — هر دو مرز inclusive


def test_compute_empty_result_semantics(client, users, source):
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    cases = [("sum", "0.0000"), ("count", "0.0000"), ("avg", None), ("min", None), ("max", None)]
    for agg, expected in cases:
        r = _create_kpi(client, tok, source["org_a"], ds_id=ds, name=f"kpi-api-e-{agg}",
                        aggregation=agg, filters=[{"field": "category", "op": "eq", "value": "nope"}])
        kpi_id = r.json()["id"]
        r = client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"]))
        assert r.status_code == 200, f"{agg}: HTTP 200 است نه خطا"
        assert r.json()["value"] == expected, agg
        assert r.json()["rows"] == 0


def test_compute_disabled_kpi_400(client, users, source):
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    r = _create_kpi(client, tok, source["org_a"], ds_id=ds, name="kpi-api-dis", enabled=False)
    kpi_id = r.json()["id"]
    r = client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"]))
    assert r.status_code == 400


def test_compute_uses_pure_engine_no_aggregation_in_router(client, users, source):
    """راستر هیچ logic تجمیعی ندارد — اگر موتور را فریب دهیم، پاسخ فریب می‌خورد.

    spy روی binding راستر نصب می‌شود (app.routers.kpis.compute_kpi) — چون راستر
    تابع را در import به namespace خودش bind کرده است.
    """
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    r = _create_kpi(client, tok, source["org_a"], ds_id=ds, name="kpi-api-pure", aggregation="count")
    kpi_id = r.json()["id"]
    import app.routers.kpis as kpi_router_module
    orig = kpi_router_module.compute_kpi
    calls = {"n": 0}
    def spy(records, definition):
        calls["n"] += 1
        return orig(records, definition)
    kpi_router_module.compute_kpi = spy
    try:
        r = client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"]))
        assert r.status_code == 200
        assert calls["n"] == 1  # compute حتماً از موتور pure گذشت
    finally:
        kpi_router_module.compute_kpi = orig


def test_no_credential_leakage_in_responses(client, users, source):
    ds = str(source["ds_id"])
    tok = _token_for(users["manager"], source["org_a"])
    r = _create_kpi(client, tok, source["org_a"], ds_id=ds, name="kpi-api-leak")
    kpi_id = r.json()["id"]
    for resp in (
        client.get("/kpis", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"])),
        client.get(f"/kpis/{kpi_id}", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"])),
        client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"])),
    ):
        body = resp.text.lower()
        assert "password" not in body and "encrypted_password" not in body
        assert "encryption_key" not in body
