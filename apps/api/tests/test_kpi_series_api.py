"""فاز ۲.۳ گام ۴ — تست‌های API سری KPI (GET /kpis/{id}/series) روی PostgreSQL واقعی

پوشش (docs/PHASE3_KPI_PLAN.md §3.5/§4/§8):
  - دسترسی analyst+ برای سری؛ viewer → 403؛ unauthenticated → 401/403
  - cross-org id → 404 (بدون افشای وجود)؛ KPI غیرفعال → 400؛ منبع map نشده → 400
  - گروه‌بندی day/week/month با کلیدهای ISO، مرتب‌سازی صعودی، deterministic
  - پنجره تاریخ دوطرفه inclusive؛ فیلترهای ساختاریافته اعمال می‌شوند
  - سری خالی → 200 با buckets == [] (سری خالی پاسخ معتبر است نه خطا)
  - مقدارها decimal-string ۴رقم — هرگز float (قرارداد گام ۲)
  - راستر هیچ logic گروه‌بندی/تجمیعی ندارد — spy ثابت می‌کند compute_kpi_series
    از موتور pure گام ۲ صدا زده می‌شود
  - سری و compute هم‌زیست‌اند: جمع bucketها == نتیجه اسکالر compute

ایزولیشن (همان درس گیت‌های قبلی): seed در fixture، override فقط داخل fixture،
cleanup فقط ردیف‌های خود ماژول (پیشوندهای kpi-series).
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

ORG_A_SLUG, ORG_B_SLUG = "org-a-kpi-series", "org-b-kpi-series"
_MODULE_EMAILS = [
    "owner-kpi-series@org-a.test", "analyst-kpi-series@org-a.test",
    "viewer-kpi-series@org-a.test", "owner-kpi-series@org-b.test",
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
            "(SELECT id FROM data_sources WHERE name LIKE :p)"), {"p": "kpi-series-%"})
        conn.execute(text(
            "DELETE FROM data_sources WHERE name LIKE :p"), {"p": "kpi-series-%"})
        conn.execute(text(
            "DELETE FROM memberships WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE slug IN (:a, :b))"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM organizations WHERE slug IN (:a, :b)"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM users WHERE email = ANY(:emails)"), {"emails": _MODULE_EMAILS})


# دیتاست ثابت سری — پنج ردیف روی چند روز/هفته/ماه:
#   2026-01-05 دوشنبه (ISO W02)، 2026-01-07 (W02)، 2026-01-12 (W03)،
#   2026-01-19 (W04)، 2026-02-02 (W06)
#   day → ۵ bucket؛ week → ۴ bucket؛ month → ۲ bucket (2026-01 = 36.25، 2026-02 = 30.25)
SERIES_ROWS = [
    ("2026-01-05", "10.5", "alpha", "a1"),
    ("2026-01-07", "1.5", "alpha", "a2"),
    ("2026-01-12", "20.0", "beta", "b1"),
    ("2026-01-19", "4.25", "gamma", "g1"),
    ("2026-02-02", "30.25", "alpha", "a3"),
]


@pytest.fixture(scope="module")
def users():
    """org A: owner + analyst + viewer؛ org B: فقط owner."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    _cleanup_own_rows()
    ids = {
        "org_a": uuid.uuid4(), "org_b": uuid.uuid4(),
        "owner": uuid.uuid4(), "analyst": uuid.uuid4(),
        "viewer": uuid.uuid4(), "b_owner": uuid.uuid4(),
    }
    emails = {
        "owner": "owner-kpi-series@org-a.test", "analyst": "analyst-kpi-series@org-a.test",
        "viewer": "viewer-kpi-series@org-a.test", "b_owner": "owner-kpi-series@org-b.test",
    }
    with seed_engine.begin() as conn:
        for key in ("owner", "analyst", "viewer", "b_owner"):
            conn.execute(text(
                "INSERT INTO users (id, email, full_name, hashed_password) VALUES (:i,:e,:f,:h)"),
                {"i": str(ids[key]), "e": emails[key], "f": "U", "h": hash_password("TestPass123")})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(ids["org_a"]), "n": "Org A Series", "s": ORG_A_SLUG})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(ids["org_b"]), "n": "Org B Series", "s": ORG_B_SLUG})
        for key, role in (("owner", "owner"), ("analyst", "analyst"), ("viewer", "viewer")):
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
    """DataSource mapped با دیتاست سری + یک منبع pending برای gate وضعیت."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    org_a = users["org_a"]
    ds_id = uuid.uuid4()
    with seed_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status) "
            "VALUES (:i,:o,'kpi-series-sales','csv',:rc,'mapped')"),
            {"i": str(ds_id), "o": str(org_a), "rc": len(SERIES_ROWS)})
        values = ", ".join(
            f"('{uuid.uuid4()}','{org_a}','{ds_id}',{m},'{d}','{c}','{l}')"
            for d, m, c, l in SERIES_ROWS
        )
        conn.execute(text(
            "INSERT INTO fact_rows (id, organization_id, data_source_id, measure_value, "
            f"dimension_date, dimension_category, dimension_label) VALUES {values}"))
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status) "
            "VALUES (:i,:o,'kpi-series-pending','csv',0,'pending')"),
            {"i": str(uuid.uuid4()), "o": str(org_a)})
    yield {"org_a": org_a, "ds_id": ds_id, "org_b": users["org_b"]}
    # cleanup در users fixture teardown


@pytest.fixture()
def client(_api_client):
    return _api_client


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _token_for(user_id, org_id):
    return create_access_token({"sub": str(user_id), "org_id": str(org_id)})


def _create_series_kpi(client, users, source, name, **overrides):
    payload = {
        "name": name,
        "data_source_id": str(source["ds_id"]),
        "aggregation": "sum",
        "filters": [],
        "group_by": "date",
        "granularity": "day",
    }
    payload.update(overrides)
    r = client.post("/kpis", json=payload, headers=_hdr(_token_for(users["owner"], source["org_a"]), source["org_a"]))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _series(client, users, source, kpi_id, role="analyst"):
    org = source["org_a"]
    return client.get(f"/kpis/{kpi_id}/series", headers=_hdr(_token_for(users[role], org), org))


# ---------- دسترسی و tenancy ----------

def test_series_requires_analyst_plus(client, users, source):
    kpi_id = _create_series_kpi(client, users, source, "kpi-series-access")
    # analyst ✓
    assert _series(client, users, source, kpi_id, role="analyst").status_code == 200
    # owner هم analyst+ است → ✓
    assert _series(client, users, source, kpi_id, role="owner").status_code == 200
    # viewer ✗ → 403
    vtok = _token_for(users["viewer"], source["org_a"])
    assert client.get(f"/kpis/{kpi_id}/series", headers=_hdr(vtok, source["org_a"])).status_code == 403
    # unauthenticated → 401/403
    assert client.get(f"/kpis/{kpi_id}/series", headers={"X-Organization-Id": str(source["org_a"])}).status_code in (401, 403)


def test_series_cross_org_is_404(client, users, source):
    kpi_id = _create_series_kpi(client, users, source, "kpi-series-xorg")
    r = client.get(f"/kpis/{kpi_id}/series",
                   headers=_hdr(_token_for(users["b_owner"], source["org_b"]), source["org_b"]))
    assert r.status_code == 404  # وجود KPI برای org B افشا نمی‌شود


def test_series_disabled_kpi_400(client, users, source):
    kpi_id = _create_series_kpi(client, users, source, "kpi-series-dis", enabled=False)
    assert _series(client, users, source, kpi_id).status_code == 400


def test_series_unmapped_source_400(client, users, source):
    # KPI روی منبع pending ساخته نمی‌شود (create خودش 400 می‌دهد) — پس منبع pending را
    # بعداً قابل‌ارجاع می‌کنیم تا gate سری را مستقل بیازماییم؟ خیر — create همین‌جا gate است.
    # این تست: series روی KPI با منبع map نشده → 400 (ساخت KPI روی pending هم 400).
    pending_id = None
    with seed_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT id FROM data_sources WHERE name = 'kpi-series-pending'")).first()
        pending_id = row[0]
    tok = _token_for(users["owner"], source["org_a"])
    r = client.post("/kpis", json={
        "name": "kpi-series-pending-att", "data_source_id": str(pending_id),
        "aggregation": "sum", "group_by": "date", "granularity": "day",
    }, headers=_hdr(tok, source["org_a"]))
    assert r.status_code == 400  # منبع map نشده اصلاً KPI نمی‌گیرد


# ---------- گروه‌بندی day/week/month ----------

def test_series_daily_grouping(client, users, source):
    kpi_id = _create_series_kpi(client, users, source, "kpi-series-day")
    r = _series(client, users, source, kpi_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_by"] == "date"
    assert body["kpi_id"] == kpi_id
    keys = [b["key"] for b in body["buckets"]]
    assert keys == ["2026-01-05", "2026-01-07", "2026-01-12", "2026-01-19", "2026-02-02"]  # صعودی
    values = {b["key"]: b["value"] for b in body["buckets"]}
    assert values["2026-01-05"] == "10.5000"  # decimal-string ۴رقم — نه float
    assert values["2026-02-02"] == "30.2500"


def test_series_weekly_grouping(client, users, source):
    kpi_id = _create_series_kpi(client, users, source, "kpi-series-week", granularity="week")
    r = _series(client, users, source, kpi_id)
    assert r.status_code == 200, r.text
    buckets = r.json()["buckets"]
    assert [(b["key"], b["value"], b["rows"]) for b in buckets] == [
        ("2026-W02", "12.0000", 2),   # 10.5 + 1.5
        ("2026-W03", "20.0000", 1),
        ("2026-W04", "4.2500", 1),
        ("2026-W06", "30.2500", 1),   # Feb 2 — iso-year/هفته درست
    ]


def test_series_monthly_grouping(client, users, source):
    kpi_id = _create_series_kpi(client, users, source, "kpi-series-month", granularity="month")
    r = _series(client, users, source, kpi_id)
    assert r.status_code == 200, r.text
    buckets = r.json()["buckets"]
    assert [(b["key"], b["value"], b["rows"]) for b in buckets] == [
        ("2026-01", "36.2500", 4),    # 10.5 + 1.5 + 20.0 + 4.25
        ("2026-02", "30.2500", 1),
    ]


# ---------- پنجره inclusive، فیلترها، خالی، دقت ----------

def test_series_inclusive_date_range(client, users, source):
    kpi_id = _create_series_kpi(client, users, source, "kpi-series-window",
                                date_from="2026-01-05", date_to="2026-01-12")
    r = _series(client, users, source, kpi_id)
    assert r.status_code == 200, r.text
    buckets = r.json()["buckets"]
    # هر دو مرز inclusive: 2026-01-05 و 2026-01-12 داخل پنجره‌اند
    assert [(b["key"], b["value"]) for b in buckets] == [
        ("2026-01-05", "10.5000"),
        ("2026-01-07", "1.5000"),
        ("2026-01-12", "20.0000"),
    ]


def test_series_filters_applied(client, users, source):
    kpi_id = _create_series_kpi(
        client, users, source, "kpi-series-filtered", granularity="week",
        filters=[{"field": "category", "op": "eq", "value": "alpha"}])
    r = _series(client, users, source, kpi_id)
    assert r.status_code == 200, r.text
    buckets = r.json()["buckets"]
    # فقط ردیف‌های alpha: W02 (10.5+1.5) و W06 (30.25)
    assert [(b["key"], b["value"], b["rows"]) for b in buckets] == [
        ("2026-W02", "12.0000", 2),
        ("2026-W06", "30.2500", 1),
    ]


def test_series_empty_returns_empty_list_200(client, users, source):
    kpi_id = _create_series_kpi(
        client, users, source, "kpi-series-empty",
        filters=[{"field": "category", "op": "eq", "value": "nope"}])
    r = _series(client, users, source, kpi_id)
    assert r.status_code == 200  # سری خالی پاسخ معتبر است نه خطا
    assert r.json()["buckets"] == []


def test_series_decimal_precision_preserved(client, users, source):
    # avg ردیف‌های alpha = (10.5 + 1.5 + 30.25) / 3 = 14.08333… → "14.0833" (HALF_UP ۴رقم)
    # group_by=category → یک bucket «alpha» با هر سه ردیف؛ هر روز جدا نمی‌شود
    kpi_id = _create_series_kpi(
        client, users, source, "kpi-series-avg", aggregation="avg",
        group_by="category", granularity=None,
        filters=[{"field": "category", "op": "eq", "value": "alpha"}])
    r = _series(client, users, source, kpi_id)
    assert r.status_code == 200, r.text
    values = [b["value"] for b in r.json()["buckets"]]
    assert values == ["14.0833"]  # رشته اعشاری دقیق — هرگز float
    for v in values:
        assert not isinstance(v, float)


def test_series_deterministic_ordering_category(client, users, source):
    kpi_id = _create_series_kpi(
        client, users, source, "kpi-series-cat", group_by="category", granularity=None)
    r1 = _series(client, users, source, kpi_id)
    r2 = _series(client, users, source, kpi_id)
    assert r1.status_code == 200 and r2.status_code == 200
    b1, b2 = r1.json()["buckets"], r2.json()["buckets"]
    assert b1 == b2  # دو فراخوانی پشت‌سرهم → خروجی یکسان
    assert [b["key"] for b in b1] == ["alpha", "beta", "gamma"]  # صعودی کدپوینتی
    assert {b["key"]: b["rows"] for b in b1} == {"alpha": 3, "beta": 1, "gamma": 1}


# ---------- موتور pure و هم‌زیستی compute ----------

def test_series_routes_through_pure_engine(client, users, source):
    """راستر هیچ logic گروه‌بندی ندارد — spy روی binding موتور pure."""
    kpi_id = _create_series_kpi(client, users, source, "kpi-series-pure")
    import app.routers.kpis as kpi_router_module
    orig = kpi_router_module.compute_kpi_series
    calls = {"n": 0}

    def spy(records, definition):
        calls["n"] += 1
        return orig(records, definition)

    kpi_router_module.compute_kpi_series = spy
    try:
        r = _series(client, users, source, kpi_id)
        assert r.status_code == 200
        assert calls["n"] == 1  # series حتماً از موتور pure گذشت
    finally:
        kpi_router_module.compute_kpi_series = orig


def test_series_and_compute_coexist(client, users, source):
    """جمع bucketهای سری == نتیجه اسکالر compute برای همان تعریف (سمانتیک موتور §3)."""
    kpi_id = _create_series_kpi(client, users, source, "kpi-series-coexist", granularity="week")
    tok = _token_for(users["analyst"], source["org_a"])
    sr = client.get(f"/kpis/{kpi_id}/series", headers=_hdr(tok, source["org_a"]))
    cr = client.post(f"/kpis/{kpi_id}/compute", headers=_hdr(tok, source["org_a"]))
    assert sr.status_code == 200 and cr.status_code == 200
    from decimal import Decimal
    bucket_total = sum(Decimal(b["value"]) for b in sr.json()["buckets"])
    assert cr.json()["value"] == f"{bucket_total:.4f}"  # "66.5000"
