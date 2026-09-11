"""فاز ۲.۵ گام ۳ — تست‌های API اندپوینت‌های AI (docs/PHASE5_AI_PLAN.md §3/§8.3) روی PostgreSQL واقعی

پوشش:
  - GET /kpis/{id}/insight: ماتریس نقش‌ها (analyst/manager/owner ✓، viewer 403،
    unauth 401/403)، cross-org → 404، disabled → 400، منبع pending → 400
  - window: پیش‌فرض ۲۸، clamp پایین ۷، clamp بالا ۳۶۵، پنجره تعریف KPI نامعتبر → 422
  - packet دستی-محاسبه‌شده: sum دو پنجره مجاور، change/change_pct با دقت Decimal،
    direction/flagged، movers مرتب (نزولی |contribution|)، دوره‌ها inclusive
  - روایت: بدون پیکربندی AI → narrative null + provider_unavailable؛ stub provider
    (seam ماژول-سطح factory) → narrative پر؛ packet هرگز credential/org دیگر ندارد
  - GET /assistant/briefing: analyst+، empty org → 200 خالی، packetها +
    flagged_count، حذف disabled/منبع pending/پنجره نامعتبر، ترتیب created_at desc،
    non-disclosure در prompt و پاسخ

هرگز LLM واقعی صدا زده نمی‌شود (§8) — فقط stub درون-پروسه‌ای.

ایزولیشن (الگوی established): seed در fixture، override فقط داخل fixture،
cleanup فقط ردیف‌های خود ماژول (پیشوندهای ins- و اسلاگ‌های org-*-ins).
"""

import json
import os
import pathlib
import sys
import uuid
from datetime import date, timedelta

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

ORG_A_SLUG, ORG_B_SLUG, ORG_C_SLUG = "org-a-ins", "org-b-ins", "org-c-ins"
_MODULE_EMAILS = [
    "owner-ins-a@org-a.test", "analyst-ins-a@org-a.test", "viewer-ins-a@org-a.test",
    "owner-ins-b@org-b.test",
    "owner-ins-c@org-c.test", "analyst-ins-c@org-c.test", "viewer-ins-c@org-c.test",
]


def _cleanup_own_rows():
    if seed_engine is None:
        return
    with seed_engine.begin() as conn:
        slugs = {"a": ORG_A_SLUG, "b": ORG_B_SLUG, "c": ORG_C_SLUG}
        conn.execute(text(
            "DELETE FROM kpi_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE slug = ANY(:slugs))"),
            {"slugs": [ORG_A_SLUG, ORG_B_SLUG, ORG_C_SLUG]})
        conn.execute(text(
            "DELETE FROM fact_rows WHERE data_source_id IN "
            "(SELECT id FROM data_sources WHERE name LIKE :p)"), {"p": "ins-%"})
        conn.execute(text(
            "DELETE FROM data_sources WHERE name LIKE :p"), {"p": "ins-%"})
        conn.execute(text(
            "DELETE FROM memberships WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE slug = ANY(:slugs))"),
            {"slugs": [ORG_A_SLUG, ORG_B_SLUG, ORG_C_SLUG]})
        conn.execute(text(
            "DELETE FROM organizations WHERE slug = ANY(:slugs)"),
            {"slugs": [ORG_A_SLUG, ORG_B_SLUG, ORG_C_SLUG]})
        conn.execute(text("DELETE FROM users WHERE email = ANY(:emails)"), {"emails": _MODULE_EMAILS})


# دیتاست نسبی به «امروز» — برای window پیش‌فرض ۲۸:
#   current  [today-27 .. today]      → ردیف‌های today-10
#   previous [today-55 .. today-28]   → ردیف‌های today-37
# org A: alpha=100 (جاری) vs 80 (قبلی)؛ beta=50 vs 10
#   current=150.0000، previous=90.0000، change=60.0000، change_pct=60/90=0.6667 → flagged/up
#   movers: beta +40.0000 سپس alpha +20.0000 (نزولی |contribution|)
def _seed_rows(org, ds):
    d_cur = (date.today() - timedelta(days=10)).isoformat()
    d_prev = (date.today() - timedelta(days=37)).isoformat()
    return [
        (d_cur, "100.0", "alpha", "a1"),
        (d_cur, "50.0", "beta", "b1"),
        (d_prev, "80.0", "alpha", "a1"),
        (d_prev, "10.0", "beta", "b1"),
    ]


@pytest.fixture(scope="module")
def users():
    """org A (insight) + org B (cross-org) + org C (briefing) با نقش‌های لازم."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    _cleanup_own_rows()
    ids = {
        "org_a": uuid.uuid4(), "org_b": uuid.uuid4(), "org_c": uuid.uuid4(),
        "owner": uuid.uuid4(), "analyst": uuid.uuid4(), "viewer": uuid.uuid4(),
        "b_owner": uuid.uuid4(),
        "c_owner": uuid.uuid4(), "c_analyst": uuid.uuid4(), "c_viewer": uuid.uuid4(),
    }
    emails = {
        "owner": "owner-ins-a@org-a.test", "analyst": "analyst-ins-a@org-a.test",
        "viewer": "viewer-ins-a@org-a.test", "b_owner": "owner-ins-b@org-b.test",
        "c_owner": "owner-ins-c@org-c.test", "c_analyst": "analyst-ins-c@org-c.test",
        "c_viewer": "viewer-ins-c@org-c.test",
    }
    with seed_engine.begin() as conn:
        for key in emails:
            conn.execute(text(
                "INSERT INTO users (id, email, full_name, hashed_password) VALUES (:i,:e,:f,:h)"),
                {"i": str(ids[key]), "e": emails[key], "f": "U", "h": hash_password("TestPass123")})
        for key, name, slug in (
            ("org_a", "Org A Insight", ORG_A_SLUG),
            ("org_b", "Org B Insight", ORG_B_SLUG),
            ("org_c", "Org C Insight", ORG_C_SLUG),
        ):
            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                         {"i": str(ids[key]), "n": name, "s": slug})
        for key, org_key, role in (
            ("owner", "org_a", "owner"), ("analyst", "org_a", "analyst"),
            ("viewer", "org_a", "viewer"), ("b_owner", "org_b", "owner"),
            ("c_owner", "org_c", "owner"), ("c_analyst", "org_c", "analyst"),
            ("c_viewer", "org_c", "viewer"),
        ):
            conn.execute(text(
                "INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:i,:u,:o,:r)"),
                {"i": str(uuid.uuid4()), "u": str(ids[key]), "o": str(ids[org_key]), "r": role})
    yield ids
    _cleanup_own_rows()


def _insert_source_with_facts(org_id, name, rows):
    ds_id = uuid.uuid4()
    with seed_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status) "
            "VALUES (:i,:o,:n,'csv',:rc,'mapped')"),
            {"i": str(ds_id), "o": str(org_id), "n": name, "rc": len(rows)})
        values = ", ".join(
            f"('{uuid.uuid4()}','{org_id}','{ds_id}',{m},'{d}','{c}','{l}')"
            for d, m, c, l in rows
        )
        conn.execute(text(
            "INSERT INTO fact_rows (id, organization_id, data_source_id, measure_value, "
            f"dimension_date, dimension_category, dimension_label) VALUES {values}"))
    return ds_id


@pytest.fixture(scope="module")
def source(users):
    """منبع mapped org A + منبع pending + یک KPI مستقیم روی منبع pending (برای gate 400)."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    org_a = users["org_a"]
    ds_id = _insert_source_with_facts(org_a, "ins-sales", _seed_rows(org_a, "ins-sales"))
    pending_id = uuid.uuid4()
    with seed_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status) "
            "VALUES (:i,:o,'ins-pending','csv',0,'pending')"),
            {"i": str(pending_id), "o": str(org_a)})
        # KPI مستقیم روی منبع pending (بایپس API) — insight باید 400 بدهد
        conn.execute(text(
            "INSERT INTO kpi_definitions (id, organization_id, data_source_id, name, "
            "aggregation, filters, enabled) VALUES (:i,:o,:d,'ins-pending-attached','sum','[]'::jsonb,true)"),
            {"i": str(uuid.uuid4()), "o": str(org_a), "d": str(pending_id)})
    yield {"org_a": org_a, "org_b": users["org_b"], "ds_id": ds_id, "pending_id": pending_id}


@pytest.fixture(scope="module")
def brief_source(users):
    """org C: منبع mapped + دیتاست برای بریفینگ."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    org_c = users["org_c"]
    ds_id = _insert_source_with_facts(org_c, "ins-brief", _seed_rows(org_c, "ins-brief"))
    yield {"org_c": org_c, "ds_id": ds_id}


@pytest.fixture()
def client(_api_client):
    return _api_client


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _token_for(user_id, org_id):
    return create_access_token({"sub": str(user_id), "org_id": str(org_id)})


def _create_kpi(client, tok, org, payload):
    r = client.post("/kpis", json=payload, headers=_hdr(tok, org))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _main_kpi_payload(source, name, **overrides):
    payload = {
        "name": name,
        "data_source_id": str(source["ds_id"]),
        "aggregation": "sum",
        "filters": [],
        "group_by": "category",
        "granularity": None,
    }
    payload.update(overrides)
    return payload


def _get_insight(client, users, source, kpi_id, role="analyst", window_days=None):
    org = source["org_a"]
    url = f"/kpis/{kpi_id}/insight"
    if window_days is not None:
        url += f"?window_days={window_days}"
    return client.get(url, headers=_hdr(_token_for(users[role], org), org))


def _get_briefing(client, users, org_key, role_key, window_days=None):
    org = users[org_key]
    url = "/assistant/briefing"
    if window_days is not None:
        url += f"?window_days={window_days}"
    return client.get(url, headers=_hdr(_token_for(users[role_key], org), org))


@pytest.fixture()
def _ai_env_off(monkeypatch):
    """AI پیکربندی‌ناپذیر برای تست «narrative null» — کلید env + settings sentinel."""
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("SAMBANOVA_API_KEY", raising=False)
    from app.core import config
    monkeypatch.setattr(config.settings, "AI_API_KEY", config.AI_API_KEY_DEV_FALLBACK, raising=False)
    yield


class _StubProvider:
    """stub provider — packet دریافتی را ضبط می‌کند (seam ماژول-سطح factory)."""

    def __init__(self):
        self.captured_json = None

    def narrate(self, packet):
        self.captured_json = json.dumps(packet, ensure_ascii=False)
        from app.services.ai.base import NarrationResult
        return NarrationResult(summary="خلاصه آزمایشی", highlights=["نکته ۱", "نکته ۲"])


@pytest.fixture()
def _stub_provider(monkeypatch):
    stub = _StubProvider()
    from app.services.ai import factory as ai_factory
    monkeypatch.setattr(ai_factory, "get_llm_provider", lambda: stub)
    return stub


# ---------- GET /kpis/{id}/insight: دسترسی و tenancy ----------

def test_insight_requires_analyst_plus(client, users, source):
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-access"))
    # analyst ✓ و manager/owner هم analyst+ اند ✓
    assert _get_insight(client, users, source, kpi_id, role="analyst").status_code == 200
    assert _get_insight(client, users, source, kpi_id, role="owner").status_code == 200
    # viewer ✗ → 403
    vtok = _token_for(users["viewer"], source["org_a"])
    assert client.get(f"/kpis/{kpi_id}/insight", headers=_hdr(vtok, source["org_a"])).status_code == 403
    # unauthenticated → 401/403
    assert client.get(f"/kpis/{kpi_id}/insight", headers={"X-Organization-Id": str(source["org_a"])}).status_code in (401, 403)


def test_insight_cross_org_is_404(client, users, source):
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-xorg"))
    r = client.get(f"/kpis/{kpi_id}/insight",
                   headers=_hdr(_token_for(users["b_owner"], source["org_b"]), source["org_b"]))
    assert r.status_code == 404  # وجود KPI برای org B افشا نمی‌شود


def test_insight_disabled_kpi_400(client, users, source):
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-disabled", enabled=False))
    assert _get_insight(client, users, source, kpi_id).status_code == 400


def test_insight_unmapped_source_400(client, users, source):
    # KPI مستقیم روی منبع pending (fixture) — هیچ محاسبه‌ای نباید انجام شود
    with seed_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT id FROM kpi_definitions WHERE name = 'ins-pending-attached'")).first()
    r = client.get(f"/kpis/{row[0]}/insight",
                   headers=_hdr(_token_for(users["analyst"], source["org_a"]), source["org_a"]))
    assert r.status_code == 400  # منبع map نشده → 400 (سمانتیک compute/series)


# ---------- window: پیش‌فرض / clamp / 422 ----------

def test_insight_default_window_28_hand_computed(client, users, source):
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-main"))
    r = _get_insight(client, users, source, kpi_id)
    assert r.status_code == 200, r.text
    body = r.json()
    today = date.today()
    n = 28
    assert body["window_days"] == n
    assert body["current_period"] == {
        "from": (today - timedelta(days=n - 1)).isoformat(), "to": today.isoformat()}
    assert body["previous_period"] == {
        "from": (today - timedelta(days=2 * n - 1)).isoformat(),
        "to": (today - timedelta(days=n)).isoformat()}
    # مقادیر دستی: current=100+50، previous=80+10
    assert body["current_value"] == "150.0000"
    assert body["previous_value"] == "90.0000"
    assert body["change"] == "60.0000"
    assert body["change_pct"] == "0.6667"  # 60/90 → HALF_UP چهار رقم
    assert body["direction"] == "up"
    assert body["flagged"] is True  # 0.6667 >= 0.25
    assert body["kpi_id"] == kpi_id


def test_insight_window_clamped_to_min_7(client, users, source):
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-clamp-low"))
    r = _get_insight(client, users, source, kpi_id, window_days=3)  # < 7 → clamp ۷
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 7
    today = date.today()
    # current [today-6..today]: بدون ردیف → sum خالی = 0.0000 (قرارداد §3.3 — نه None)؛
    # previous [today-13..today-7]: ردیف today-10 داخل → 150.0000
    assert body["current_period"] == {
        "from": (today - timedelta(days=6)).isoformat(), "to": today.isoformat()}
    assert body["current_value"] == "0.0000"
    assert body["previous_value"] == "150.0000"
    assert body["change"] == "-150.0000"
    assert body["change_pct"] == "-1.0000"  # -150/150
    assert body["direction"] == "down"
    assert body["flagged"] is True  # |−1.0| >= 0.25


def test_insight_window_clamped_to_max_365(client, users, source):
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-clamp-high"))
    r = _get_insight(client, users, source, kpi_id, window_days=1000)  # > 365 → clamp ۳۶۵
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 365
    # current شامل هر دو روز seed است (today-10 و today-37 هر دو داخل ۳۶۵ روز) →
    # sum = 100+50+80+10 = 240.0000؛ previous (۳۶۵ روز قبل‌تر) خالی → sum = 0.0000؛
    # previous=0 → change_pct=None (تقسیم بر صفر وجود ندارد §2.1) → up بدون پرچم
    assert body["current_value"] == "240.0000"
    assert body["previous_value"] == "0.0000"
    assert body["change"] == "240.0000"
    assert body["change_pct"] is None
    assert body["direction"] == "up"
    assert body["flagged"] is False


def test_insight_definition_window_too_narrow_422(client, users, source):
    today = date.today()
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-narrow",
                          date_from=(today - timedelta(days=8)).isoformat(),
                          date_to=(today - timedelta(days=6)).isoformat()))
    r = _get_insight(client, users, source, kpi_id)
    assert r.status_code == 422  # پنجره تعریف دو پنجره کامل ۲۸روزه نمی‌پذیرد (§8.3)
    assert "window" in r.json()["detail"].lower()


# ---------- packet: خالی/دقت/ترتیب/determinism ----------

def test_insight_empty_series_returns_unknown_contract(client, users, source):
    tok = _token_for(users["owner"], source["org_a"])
    # avg روی پنجره خالی → None (قرارداد §3.3) → unknown §2.1 (داده ناکافی، بدون عدد ساختگی)
    kpi_id = _create_kpi(
        client, tok, source["org_a"],
        _main_kpi_payload(source, "ins-empty-avg", aggregation="avg",
                          filters=[{"field": "category", "op": "eq", "value": "nope"}]))
    body = _get_insight(client, users, source, kpi_id).json()
    assert body["current_value"] is None and body["previous_value"] is None
    assert body["change"] is None and body["change_pct"] is None
    assert body["direction"] == "unknown" and body["flagged"] is False
    assert body["top_movers"] == []
    # sum روی پنجره خالی → 0.0000/0.0000 → تغییر exact صفر → flat (قرارداد §2.1)
    kpi_sum = _create_kpi(
        client, tok, source["org_a"],
        _main_kpi_payload(source, "ins-empty-sum",
                          filters=[{"field": "category", "op": "eq", "value": "nope"}]))
    body_sum = _get_insight(client, users, source, kpi_sum).json()
    assert body_sum["current_value"] == "0.0000" and body_sum["previous_value"] == "0.0000"
    # change exact صفر → flat؛ 0/0 تعریف‌نشده → change_pct=None (قرارداد §2.1)
    assert body_sum["change"] == "0.0000" and body_sum["change_pct"] is None
    assert body_sum["direction"] == "flat" and body_sum["flagged"] is False


def test_insight_decimal_strings_never_float(client, users, source):
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-dec"))
    body = _get_insight(client, users, source, kpi_id).json()
    for field in ("current_value", "previous_value", "change", "change_pct"):
        assert body[field] is None or isinstance(body[field], str)
        assert not isinstance(body[field], float)
    for m in body["top_movers"]:
        assert isinstance(m["value"], str) and isinstance(m["contribution"], str)


def test_insight_movers_ordered_desc_contribution(client, users, source):
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-movers"))
    body = _get_insight(client, users, source, kpi_id).json()
    # beta +40.0000 |40| > alpha +20.0000 |20| — نزولی |contribution|
    assert [(m["key"], m["value"], m["contribution"]) for m in body["top_movers"]] == [
        ("beta", "50.0000", "40.0000"),
        ("alpha", "100.0000", "20.0000"),
    ]


def test_insight_deterministic_two_calls_identical(client, users, source):
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-det"))
    b1 = _get_insight(client, users, source, kpi_id).json()
    b2 = _get_insight(client, users, source, kpi_id).json()
    assert b1 == b2  # پاسخ insight هیچ مؤلفه زمانی غیر-deterministic ندارد


# ---------- روایت: unconfigured → null؛ stub → پر؛ non-disclosure ----------

def test_insight_narrative_null_when_unconfigured(client, users, source, _ai_env_off):
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-nonarr"))
    body = _get_insight(client, users, source, kpi_id).json()
    # packet deterministic سالم است — فقط روایت null می‌شود (فروپاشی نرم §3)
    assert body["current_value"] == "150.0000"
    assert body["narrative"] is None
    assert body["narrative_error"] == "provider_unavailable"


def test_insight_stub_provider_narrative_and_prompt_non_disclosure(client, users, source, _stub_provider):
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-stub"))
    r = _get_insight(client, users, source, kpi_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["narrative"] == {"summary": "خلاصه آزمایشی", "highlights": ["نکته ۱", "نکته ۲"]}
    assert body["narrative_error"] is None
    # prompt payload هرگز credential/org دیگر ندارد (§2.4/§5)
    captured = _stub_provider.captured_json
    assert captured is not None
    for forbidden in ("password", "encrypted_password", "host", "dbname", "dsn",
                      "authorization", ORG_B_SLUG, str(source["org_b"])):
        assert forbidden not in captured.lower()
    assert "ins-stub" in captured  # packet همان KPI است


def test_insight_stub_provider_error_never_500(client, users, source, monkeypatch):
    from app.services.ai import factory as ai_factory
    from app.services.ai.base import AiProviderError

    class _BrokenProvider:
        def narrate(self, packet):
            raise AiProviderError("boom-sanitized")

    monkeypatch.setattr(ai_factory, "get_llm_provider", lambda: _BrokenProvider())
    kpi_id = _create_kpi(
        client, _token_for(users["owner"], source["org_a"]), source["org_a"],
        _main_kpi_payload(source, "ins-broken"))
    r = _get_insight(client, users, source, kpi_id)
    assert r.status_code == 200  # خطای provider هرگز 500 نمی‌شود
    body = r.json()
    assert body["narrative"] is None
    assert body["narrative_error"] == "provider_error"
    assert body["current_value"] == "150.0000"  # لایه deterministic دست‌نخورده


# ---------- GET /assistant/briefing ----------

def test_briefing_requires_analyst_plus(client, users, brief_source):
    # analyst ✓ / owner ✓ / viewer 403 / unauth 401-403
    assert _get_briefing(client, users, "org_c", "c_analyst").status_code == 200
    assert _get_briefing(client, users, "org_c", "c_owner").status_code == 200
    assert _get_briefing(client, users, "org_c", "c_viewer").status_code == 403
    assert client.get("/assistant/briefing",
                      headers={"X-Organization-Id": str(users["org_c"])}).status_code in (401, 403)


def test_briefing_empty_org_returns_empty_200(client, users):
    r = _get_briefing(client, users, "org_b", "b_owner")  # org B هیچ KPI ندارد
    assert r.status_code == 200
    body = r.json()
    assert body["kpis"] == []
    assert body["flagged_count"] == 0
    assert body["window_days"] == 28  # پیش‌فرض
    assert body["generated_at"]  # ISO-8601 UTC


def test_briefing_packets_flagged_count_and_ordering(client, users, brief_source):
    org_c = brief_source["org_c"]
    tok = _token_for(users["c_owner"], org_c)
    k1 = _create_kpi(client, tok, org_c, _main_kpi_payload(brief_source, "ins-brief-alpha"))
    k2 = _create_kpi(client, tok, org_c, _main_kpi_payload(brief_source, "ins-brief-zeta"))
    r = _get_briefing(client, users, "org_c", "c_analyst")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 28
    assert [k["kpi_id"] for k in body["kpis"]] == [k2, k1]  # created_at desc — مثل GET /kpis
    alpha = next(k for k in body["kpis"] if k["kpi_id"] == k1)
    assert alpha["current_value"] == "150.0000"
    assert alpha["previous_value"] == "90.0000"
    assert alpha["change"] == "60.0000" and alpha["change_pct"] == "0.6667"
    assert alpha["direction"] == "up" and alpha["flagged"] is True
    assert [(m["key"], m["contribution"]) for m in alpha["top_movers"]] == [
        ("beta", "40.0000"), ("alpha", "20.0000")]
    assert body["flagged_count"] == 2  # هر دو KPI پرچم‌دار (همان داده)


def test_briefing_excludes_disabled_kpi(client, users, brief_source):
    org_c = brief_source["org_c"]
    tok = _token_for(users["c_owner"], org_c)
    k1 = _create_kpi(client, tok, org_c, _main_kpi_payload(brief_source, "ins-brief-on"))
    k2 = _create_kpi(client, tok, org_c, _main_kpi_payload(brief_source, "ins-brief-off"))
    r = client.patch(f"/kpis/{k2}", json={"enabled": False}, headers=_hdr(tok, org_c))
    assert r.status_code == 200, r.text
    body = _get_briefing(client, users, "org_c", "c_analyst").json()
    ids = [k["kpi_id"] for k in body["kpis"]]
    assert k1 in ids and k2 not in ids  # disabled حذف می‌شود


def test_briefing_excludes_unmapped_source_and_narrow_window(client, users, source, brief_source):
    # org A: KPI مستقیم روی منبع pending (fixture) + KPI با پنجره تنگ → هر دو از بریفینگ حذف
    tok = _token_for(users["owner"], source["org_a"])
    today = date.today()
    _create_kpi(client, tok, source["org_a"],
                _main_kpi_payload(source, "ins-brief-narrow",
                                  date_from=(today - timedelta(days=5)).isoformat(),
                                  date_to=today.isoformat()))
    body = _get_briefing(client, users, "org_a", "analyst").json()
    names = [k["kpi_name"] for k in body["kpis"]]
    assert "ins-pending-attached" not in names
    assert "ins-brief-narrow" not in names
    # KPI سالم org A همچنان داخل است
    assert "ins-main" in names or "ins-access" in names or len(names) >= 1


def test_briefing_window_clamp_and_narrative_null(client, users, brief_source, _ai_env_off):
    r = _get_briefing(client, users, "org_c", "c_analyst", window_days=2)  # → clamp ۷
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 7
    # current [today-6..today] بدون ردیف → sum = 0.0000؛ previous [today-13..today-7]
    # شامل today-10 → 150.0000 → down/flagged برای هر KPI sum (همان داده)
    assert all(k["direction"] == "down" and k["flagged"] for k in body["kpis"])
    # flagged_count = تعداد packetهای پرچم‌دار (مستقل از ترتیب اجرای تست‌های ماژول)
    assert body["flagged_count"] == sum(1 for k in body["kpis"] if k["flagged"])
    assert body["flagged_count"] >= 2  # حداقل دو KPI sum روی داده مشترک org C
    assert body["narrative"] is None
    assert body["narrative_error"] == "provider_unavailable"


def test_briefing_stub_provider_narrative_and_prompt_non_disclosure(client, users, brief_source, _stub_provider):
    r = _get_briefing(client, users, "org_c", "c_analyst")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["narrative"] == {"summary": "خلاصه آزمایشی", "highlights": ["نکته ۱", "نکته ۲"]}
    assert body["narrative_error"] is None
    captured = _stub_provider.captured_json
    assert captured is not None
    packet = json.loads(captured)
    assert packet["kind"] == "briefing"
    assert packet["flagged_count"] == body["flagged_count"]
    assert len(packet["kpis"]) == len(body["kpis"])
    # هیچ credential/org دیگر در prompt نیست (§2.4/§5)
    for forbidden in ("password", "encrypted_password", "host", "dbname", "dsn",
                      ORG_A_SLUG, ORG_B_SLUG, str(users["org_a"]), str(users["org_b"])):
        assert forbidden not in captured


def test_numeric_wire_fields_are_decimal_strings(client, users, brief_source):
    body = _get_briefing(client, users, "org_c", "c_analyst").json()
    for k in body["kpis"]:
        for field in ("current_value", "previous_value", "change", "change_pct"):
            assert k[field] is None or isinstance(k[field], str)
            assert not isinstance(k[field], float)
