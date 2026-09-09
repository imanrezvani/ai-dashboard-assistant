"""فاز ۲.۳ گام ۱ — تست‌های RLS و tenant isolation برای kpi_definitions روی PostgreSQL واقعی

SQLite هیچ RLS ندارد؛ این ماژول فقط روی PostgreSQL واقعی اجرا می‌شود (بدون DB → skip).
پوشش: ENABLE + FORCE + پالیسی Fail-Closed org-scoped، درج بدون context رد می‌شود
(WITH CHECK)، org B هیچ KPIای از org A نمی‌بیند، FK CASCADE حذف منبع، schema ستون‌ها.

ایزولیشن (درس گیت‌های قبلی): seed در fixture (زمان اجرا)، cleanup فقط ردیف‌های خود
ماژول (پیشوند kpi-)، بدون drop_all، بدون دستکاری import-time state مشترک.
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
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import DataSource
from app.models.kpi_definition import KpiDefinition


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

# RLS برای kpi_definitions — دقیقاً همان DDL اپ (app/main.py)؛ idempotent
with engine.begin() as conn:
    conn.execute(text("ALTER TABLE kpi_definitions ENABLE ROW LEVEL SECURITY"))
    conn.execute(text("ALTER TABLE kpi_definitions FORCE ROW LEVEL SECURITY"))
    conn.execute(text("DROP POLICY IF EXISTS tenant_isolation ON kpi_definitions"))
    conn.execute(text(
        "CREATE POLICY tenant_isolation ON kpi_definitions FOR ALL "
        "USING (organization_id::text = current_setting('app.current_org_id', true)) "
        "WITH CHECK (organization_id::text = current_setting('app.current_org_id', true))"
    ))

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


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

ORG_A_SLUG, ORG_B_SLUG = "org-a-kpi", "org-b-kpi"
_MODULE_EMAILS = ["kpi-owner-a@test.local", "kpi-owner-b@test.local", "kpi-tmp-%@test.local"]


def _cleanup_own_rows():
    """حذف فقط ردیف‌های این ماژول — rerun-safe (درس UniqueViolation گیت فاز ۲.۱)."""
    if seed_engine is None:
        return
    with seed_engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM kpi_definitions WHERE name LIKE :p OR organization_id IN "
            "(SELECT id FROM organizations WHERE slug IN (:a, :b))"), {"p": "kpi-%", "a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM data_sources WHERE name LIKE :p"), {"p": "kpi-%"})
        conn.execute(text(
            "DELETE FROM memberships WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE slug IN (:a, :b))"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM organizations WHERE slug IN (:a, :b)"), {"a": ORG_A_SLUG, "b": ORG_B_SLUG})
        conn.execute(text("DELETE FROM users WHERE email LIKE :p"), {"p": "kpi-%@test.local"})


@pytest.fixture(scope="module")
def env():
    """دو org با یک mapped source هر کدام — seed در زمان اجرا."""
    if seed_engine is None:
        pytest.skip("superuser engine برای seed در دسترس نیست")
    _cleanup_own_rows()
    ids = {
        "org_a": uuid.uuid4(), "org_b": uuid.uuid4(),
        "user_a": uuid.uuid4(), "user_b": uuid.uuid4(),
        "ds_a": uuid.uuid4(), "ds_b": uuid.uuid4(),
    }
    with seed_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, email, full_name, hashed_password) VALUES (:i,:e,:f,:h)"),
            {"i": str(ids["user_a"]), "e": "kpi-owner-a@test.local", "f": "A", "h": "x"})
        conn.execute(text(
            "INSERT INTO users (id, email, full_name, hashed_password) VALUES (:i,:e,:f,:h)"),
            {"i": str(ids["user_b"]), "e": "kpi-owner-b@test.local", "f": "B", "h": "x"})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(ids["org_a"]), "n": "Org A KPI", "s": ORG_A_SLUG})
        conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:i,:n,:s)"),
                     {"i": str(ids["org_b"]), "n": "Org B KPI", "s": ORG_B_SLUG})
        conn.execute(text(
            "INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:i,:u,:o,'owner')"),
            {"i": str(uuid.uuid4()), "u": str(ids["user_a"]), "o": str(ids["org_a"])})
        conn.execute(text(
            "INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:i,:u,:o,'owner')"),
            {"i": str(uuid.uuid4()), "u": str(ids["user_b"]), "o": str(ids["org_b"])})
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status) "
            "VALUES (:i,:o,:n,:f,:r,:s)"),
            {"i": str(ids["ds_a"]), "o": str(ids["org_a"]), "n": "kpi-src-a", "f": "csv", "r": 3, "s": "mapped"})
        conn.execute(text(
            "INSERT INTO data_sources (id, organization_id, name, file_type, row_count, status) "
            "VALUES (:i,:o,:n,:f,:r,:s)"),
            {"i": str(ids["ds_b"]), "o": str(ids["org_b"]), "n": "kpi-src-b", "f": "csv", "r": 3, "s": "mapped"})
    yield ids
    _cleanup_own_rows()


def _make_kpi(env_ids, *, name: str = "kpi-test", **overrides) -> KpiDefinition:
    payload = dict(
        organization_id=env_ids["org_a"],
        data_source_id=env_ids["ds_a"],
        name=name,
        aggregation="sum",
        filters=[],
    )
    payload.update(overrides)
    return KpiDefinition(**payload)


# ---------- RLS runtime ----------

def test_rls_enabled_and_force(env):
    with seed_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='kpi_definitions'")).fetchone()
        assert row is not None
        assert row.relrowsecurity is True, "kpi_definitions must have RLS ENABLED"
        assert row.relforcerowsecurity is True, "kpi_definitions must have FORCE ROW LEVEL SECURITY"


def test_policy_fail_closed_and_org_scoped(env):
    with seed_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) "
            "FROM pg_policy WHERE polname='tenant_isolation' "
            "AND polrelid='kpi_definitions'::regclass")).fetchone()
        assert row is not None, "tenant_isolation policy on kpi_definitions not found"
        qual, chk = str(row[0]), str(row[1])
        for expr in (qual, chk):
            # pg_get_expr literalها را ::text cast می‌کند — نرمالایز قبل از مقایسه
            normalized = expr.replace("'::text", "'").upper()
            assert "IS NULL" not in normalized, f"policy must be fail-closed, got: {expr}"
            assert "CURRENT_SETTING('APP.CURRENT_ORG_ID', TRUE)" in normalized
            assert "ORGANIZATION_ID" in normalized


def test_insert_without_context_rejected_by_with_check(env):
    """درج بدون tenant context → WITH CHECK رد می‌کند (Fail-Closed در عمل)."""
    with SessionLocal() as db:
        db.add(_make_kpi(env, name="kpi-noctx"))
        with pytest.raises(Exception):
            db.commit()
        db.rollback()


def test_org_sees_only_own_rows(env):
    with SessionLocal() as db:
        # درج با context org A
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(env["org_a"])})
        k1 = _make_kpi(env, name="kpi-own-a")
        k2 = _make_kpi(env, name="kpi-own-a2", group_by="date", granularity="month")
        db.add_all([k1, k2])
        db.commit()
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(env["org_a"])})
        assert db.query(KpiDefinition).count() == 2
        assert all(str(k.organization_id) == str(env["org_a"]) for k in db.query(KpiDefinition).all())
        db.rollback()


def test_cross_organization_access_blocked(env):
    """org B هیچ KPIای از org A نمی‌بیند (INSERT هم با context org B روی منبع org A رد می‌شود)."""
    with SessionLocal() as db:
        # org A دو KPI دارد
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(env["org_a"])})
        db.add(_make_kpi(env, name="kpi-xorg-a"))
        db.commit()
        db.rollback()

        # org B: صفر ردیف
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(env["org_b"])})
        assert db.query(KpiDefinition).count() == 0, "org B must not see org A KPIs"
        # درج KPI با context org B اما data_source_id منبع org A → FK مستقل از RLS رد می‌کند
        db.add(_make_kpi(env, name="kpi-xorg-b"))
        with pytest.raises(Exception):
            db.commit()
        db.rollback()


def test_source_cascade_on_real_postgres(env):
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(env["org_a"])})
        db.add(_make_kpi(env, name="kpi-cascade"))
        db.commit()
        db.rollback()
    with seed_engine.begin() as conn:
        conn.execute(text("DELETE FROM data_sources WHERE id = :i"), {"i": str(env["ds_a"])})
    with seed_engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM kpi_definitions WHERE data_source_id = :i"),
            {"i": str(env["ds_a"])}).fetchone()
        assert n[0] == 0, "KPIs must cascade-delete with their data source"


def test_migration_schema_columns():
    """parity ستون‌ها: nullable، JSONB filters، unique per-org، ایندکس‌ها."""
    with seed_engine.connect() as conn:
        cols = conn.execute(text(
            "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
            "WHERE table_name='kpi_definitions' ORDER BY column_name")).fetchall()
        by_name = {c.column_name: (c.is_nullable, c.data_type) for c in cols}
        expected = {
            "id": ("NO", "uuid"), "organization_id": ("NO", "uuid"),
            "data_source_id": ("NO", "uuid"), "name": ("NO", "character varying"),
            "description": ("YES", "character varying"), "aggregation": ("NO", "character varying"),
            "filters": ("NO", "jsonb"), "group_by": ("YES", "character varying"),
            "granularity": ("YES", "character varying"), "date_from": ("YES", "date"),
            "date_to": ("YES", "date"), "enabled": ("NO", "boolean"),
            "created_at": ("NO", "timestamp with time zone"),
            "updated_at": ("NO", "timestamp with time zone"),
        }
        assert by_name == expected, f"schema mismatch: {by_name}"
        uq = conn.execute(text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_name='kpi_definitions' AND constraint_name='uq_kpi_org_name'")).fetchone()
        assert uq is not None
        idx = conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename='kpi_definitions' "
            "AND indexname IN ('ix_kpi_definitions_organization_id','ix_kpi_definitions_data_source_id')")).fetchall()
        assert len(idx) == 2
