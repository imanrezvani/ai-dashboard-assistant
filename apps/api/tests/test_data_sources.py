"""
Stage 2 — تست اتصال داده روی PostgreSQL واقعی (Neon)

۱. CSV نمونه آپلود → Map → تأیید ردیف‌ها در fact_rows با organization_id درست
۲. کاربر Org دیگر نمی‌تواند به data_sources یا fact_rows این Org دسترسی داشته باشد
"""

import os
import io
import uuid
import pathlib
import sys

API_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.core.database import Base, get_db
import app.core.database as db_module
from app.core.security import create_access_token, hash_password
from app.models import Membership, Organization, RoleEnum, User
from app.models.data_source import DataSource
from app.models.fact_row import FactRow
from app.main import app


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
    # برای Neon: اول tasmim_app روی همان هاست
    if env_test:
        if "tasmim_app" in env_test:
            yield env_test
        try:
            yield _make_app_url(env_test, "tasmim_app", "tasmim_app_secret")
        except Exception:
            pass
        yield env_test
    if env_admin:
        try:
            yield _make_app_url(env_admin, "tasmim_app", "tasmim_app_secret")
        except Exception:
            pass
        yield env_admin
    yield "postgresql+psycopg://tasmim_app:tasmim_app_secret@localhost:5432/tasmim_yar"
    yield "postgresql+psycopg://tasmim:tasmim_secret@localhost:5432/tasmim_yar"


def _get_test_engine():
    last_err = None
    for url in _candidate_urls():
        try:
            eng = create_engine(url, pool_pre_ping=True)
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
            print(f"[test_data_sources] connected with {url}")
            return eng, url
        except Exception as e:
            last_err = e
            continue
    pytest.skip(f"PostgreSQL در دسترس نیست ({last_err}); نیاز به Neon یا docker postgres", allow_module_level=True)
    raise RuntimeError(last_err)


engine, used_url = _get_test_engine()
is_app_user = "tasmim_app" in used_url

# اطمینان از وجود جداول
Base.metadata.create_all(bind=engine)

# اگر با superuser وصل شدیم، grant بده و سپس به tasmim_app سوئیچ کن
if not is_app_user:
    try:
        with engine.begin() as c:
            c.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO tasmim_app"))
            c.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO tasmim_app"))
    except Exception as e:
        print(f"[grant warn] {e}")
    # try switch to app user
    for candidate in [_make_app_url(used_url, "tasmim_app", "tasmim_app_secret")] if "localhost" in used_url else []:
        try:
            ae = create_engine(candidate)
            with ae.connect() as c:
                c.execute(text("SELECT 1"))
            engine = ae
            used_url = candidate
            is_app_user = True
            Base.metadata.create_all(bind=engine)
            print(f"[switched to app] {candidate}")
            break
        except Exception as e:
            print(f"switch fail {e}")

# برای Neon: اگر هنوز superuser هستیم، سعی کن با tasmim_app روی همان Neon هاست وصل شوی
if not is_app_user and ("neon.tech" in used_url or os.getenv("TEST_DATABASE_URL")):
    for base in [os.getenv("TEST_DATABASE_URL"), os.getenv("ADMIN_DATABASE_URL")]:
        if not base:
            continue
        try:
            app_url = _make_app_url(base, "tasmim_app", "tasmim_app_secret")
            ae = create_engine(app_url)
            with ae.connect() as c:
                c.execute(text("SELECT 1"))
            engine = ae
            used_url = app_url
            is_app_user = True
            Base.metadata.create_all(bind=engine)
            print(f"[switched to Neon app] {app_url[:60]}")
            break
        except Exception as e:
            print(f"Neon switch fail {e}")

# فعال‌سازی RLS برای جداول جدید (دقیقا مثل main.py — memberships با fallback user)
with engine.begin() as conn:
    rls_policies = {
        "memberships": """
            CREATE POLICY tenant_isolation ON memberships
            FOR ALL USING (
                organization_id::text = current_setting('app.current_org_id', true)
                OR (
                    current_setting('app.current_org_id', true) = ''
                    AND user_id::text = current_setting('app.current_user_id', true)
                )
            )
            WITH CHECK (
                organization_id::text = current_setting('app.current_org_id', true)
                OR (
                    current_setting('app.current_org_id', true) = ''
                    AND user_id::text = current_setting('app.current_user_id', true)
                )
            );
        """,
        "data_sources": """
            CREATE POLICY tenant_isolation ON data_sources
            FOR ALL USING (organization_id::text = current_setting('app.current_org_id', true))
            WITH CHECK (organization_id::text = current_setting('app.current_org_id', true));
        """,
        "fact_rows": """
            CREATE POLICY tenant_isolation ON fact_rows
            FOR ALL USING (organization_id::text = current_setting('app.current_org_id', true))
            WITH CHECK (organization_id::text = current_setting('app.current_org_id', true));
        """,
    }
    for table, policy_sql in rls_policies.items():
        conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        conn.execute(text(policy_sql))

# grant after RLS
if is_app_user:
    # ensure app can access tables it just created (owner is itself, so ok)
    pass
else:
    with engine.begin() as c:
        try:
            c.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO tasmim_app"))
        except Exception:
            pass

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
# patch app's get_db to use this engine
db_module.engine = engine
db_module.SessionLocal = SessionLocal

def override_get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)

# helper to create user/org via DB directly (bypass API for speed)
# Use ADMIN engine for seeding if needed to bypass RLS; here we use same engine but with SET LOCAL
# For seeding we will use superuser if available, else use app with SET LOCAL

def _seed_admin_engine():
    # try to get superuser engine (neondb_owner)
    for url in [os.getenv("ADMIN_DATABASE_URL"), os.getenv("TEST_DATABASE_URL")]:
        if not url:
            continue
        if "tasmim_app" in url:
            continue
        try:
            se = create_engine(url)
            with se.connect() as c:
                c.execute(text("SELECT 1"))
            return se
        except Exception:
            continue
    return engine

seed_engine = _seed_admin_engine()
SeedSession = sessionmaker(bind=seed_engine, autocommit=False, autoflush=False)

def create_user_org(email: str, org_name: str, slug: str):
    # use seed_engine to bypass RLS (superuser)
    db = SeedSession()
    try:
        # check superuser
        is_super = False
        try:
            with seed_engine.connect() as c:
                r = c.execute(text("SELECT usesuper FROM pg_user WHERE usename=current_user")).fetchone()
                is_super = bool(r and r[0])
                # also check bypass: if not super but has bypass, still can bypass RLS
                # for seeding we can use direct insert via superuser; if not super, we will SET LOCAL
        except Exception:
            pass
        user_id = uuid.uuid4()
        org_id = uuid.uuid4()
        if is_super:
            with seed_engine.begin() as conn:
                conn.execute(text("INSERT INTO users (id, email, full_name, hashed_password) VALUES (:id, :email, :fn, :hp)"),
                             {"id": str(user_id), "email": email, "fn": "Test User", "hp": hash_password("TestPass123")})
                conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
                             {"id": str(org_id), "name": org_name, "slug": slug})
                conn.execute(text("INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:id, :uid, :oid, :role)"),
                             {"id": str(uuid.uuid4()), "uid": str(user_id), "oid": str(org_id), "role": "owner"})
        else:
            # use app engine with SET LOCAL
            with engine.begin() as conn:
                conn.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(org_id)})
                # need to insert user/org first (they are not RLS)
                conn.execute(text("INSERT INTO users (id, email, full_name, hashed_password) VALUES (:id, :email, :fn, :hp)"),
                             {"id": str(user_id), "email": email, "fn": "Test User", "hp": hash_password("TestPass123")})
                conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
                             {"id": str(org_id), "name": org_name, "slug": slug})
                # now membership with context
                conn.execute(text("INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:id, :uid, :oid, :role)"),
                             {"id": str(uuid.uuid4()), "uid": str(user_id), "oid": str(org_id), "role": "owner"})
        # fetch via app engine
        db2 = SessionLocal()
        try:
            u = db2.get(User, user_id)
            o = db2.get(Organization, org_id)
            return u, o
        finally:
            db2.close()
    finally:
        db.close()

# clean before tests
with seed_engine.begin() as conn:
    # delete in order due to FK
    try:
        conn.execute(text("DELETE FROM fact_rows"))
    except Exception:
        pass
    try:
        conn.execute(text("DELETE FROM data_sources"))
    except Exception:
        pass
    try:
        conn.execute(text("DELETE FROM memberships"))
    except Exception:
        pass
    try:
        conn.execute(text("DELETE FROM organizations"))
    except Exception:
        pass
    try:
        conn.execute(text("DELETE FROM users"))
    except Exception:
        pass

user_a, org_a = create_user_org("alice-ds@org-a.test", "Org A DS", "org-a-ds")
user_b, org_b = create_user_org("bob-ds@org-b.test", "Org B DS", "org-b-ds")

def token_for(user, org):
    return create_access_token({"sub": str(user.id), "org_id": str(org.id)})

token_a = token_for(user_a, org_a)
token_b = token_for(user_b, org_b)

# sample CSV
CSV_CONTENT = """date,category,amount,label
2024-01-01,Food,100.5,Apple
2024-01-02,Food,200.0,Banana
2024-01-03,Drink,50.25,Cola
"""

def test_upload_preview_and_map():
    """آپلود CSV، Map، و تأیید fact_rows با organization_id درست"""
    # upload as org A
    files = {"file": ("sample.csv", io.BytesIO(CSV_CONTENT.encode()), "text/csv")}
    resp = client.post("/data-sources/upload", files=files, headers={"Authorization": f"Bearer {token_a}", "X-Organization-Id": str(org_a.id)})
    assert resp.status_code == 200, f"upload failed {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "columns" in data and "preview" in data
    assert set(data["columns"]) == {"date", "category", "amount", "label"}
    assert len(data["preview"]) == 3
    assert data["row_count"] == 3
    ds_id = data["id"]

    # map
    map_payload = {"measure_column": "amount", "date_column": "date", "category_column": "category", "label_column": "label"}
    resp2 = client.post(f"/data-sources/{ds_id}/map", json=map_payload, headers={"Authorization": f"Bearer {token_a}", "X-Organization-Id": str(org_a.id)})
    assert resp2.status_code == 200, f"map failed {resp2.status_code}: {resp2.text}"
    assert resp2.json()["inserted_rows"] == 3

    # verify fact_rows via API
    resp3 = client.get(f"/data-sources/{ds_id}/rows", headers={"Authorization": f"Bearer {token_a}", "X-Organization-Id": str(org_a.id)})
    assert resp3.status_code == 200
    rows = resp3.json()
    assert len(rows) == 3
    # check values
    amounts = sorted([r["measure_value"] for r in rows])
    assert amounts == [50.25, 100.5, 200.0]

    # direct DB check: organization_id must be org_a, and RLS must enforce
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(org_a.id)})
        facts = db.query(FactRow).all()
        assert len(facts) == 3
        assert all(str(f.organization_id) == str(org_a.id) for f in facts)
        assert all(str(f.data_source_id) == ds_id for f in facts)
        db.rollback()
        # without context -> 0 (fail-closed)
        db.execute(text("SELECT set_config('app.current_org_id', '', true)"))
        facts_empty = db.query(FactRow).all()
        assert len(facts_empty) == 0
        db.rollback()

def test_cross_org_cannot_access_data_sources():
    """کاربر Org B نباید به data_sources/fact_rows Org A دسترسی داشته باشد"""
    # list as org A should have 1
    resp_a = client.get("/data-sources", headers={"Authorization": f"Bearer {token_a}", "X-Organization-Id": str(org_a.id)})
    assert resp_a.status_code == 200
    assert len(resp_a.json()) >= 1
    ds_id_a = resp_a.json()[0]["id"]

    # org B list should be empty (or not contain A's)
    resp_b = client.get("/data-sources", headers={"Authorization": f"Bearer {token_b}", "X-Organization-Id": str(org_b.id)})
    assert resp_b.status_code == 200
    assert all(d["id"] != ds_id_a for d in resp_b.json()), "Org B نباید فهرست Org A را ببیند"

    # direct fetch cross-org should 404
    resp = client.get(f"/data-sources/{ds_id_a}", headers={"Authorization": f"Bearer {token_b}", "X-Organization-Id": str(org_b.id)})
    assert resp.status_code in (404, 403), f"cross-org get should be 404/403, got {resp.status_code}: {resp.text}"

    # rows cross-org
    resp = client.get(f"/data-sources/{ds_id_a}/rows", headers={"Authorization": f"Bearer {token_b}", "X-Organization-Id": str(org_b.id)})
    assert resp.status_code in (404, 403), f"cross-org rows should be 404/403, got {resp.status_code}"

    # DB level RLS: query without WHERE as org B should not see A's rows
    with SessionLocal() as db:
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(org_b.id)})
        # org B has no fact_rows yet, so count 0
        facts_b = db.query(FactRow).all()
        assert len(facts_b) == 0, f"Org B should see 0 fact_rows, got {len(facts_b)}"
        # org B tries to query data_sources without WHERE
        ds_b = db.query(DataSource).all()
        assert len(ds_b) == 0, f"Org B should see 0 data_sources, got {len(ds_b)}"
        db.rollback()
        # org A still sees its rows
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(org_a.id)})
        facts_a = db.query(FactRow).all()
        assert len(facts_a) == 3
        db.rollback()

def test_excel_upload_preview():
    """تأیید آپلود Excel هم کار می‌کند"""
    # create simple excel in memory
    import pandas as pd
    df = pd.DataFrame({"date": ["2024-02-01", "2024-02-02"], "category": ["X", "Y"], "amount": [10, 20], "label": ["a", "b"]})
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    files = {"file": ("sample.xlsx", buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    resp = client.post("/data-sources/upload", files=files, headers={"Authorization": f"Bearer {token_a}", "X-Organization-Id": str(org_a.id)})
    assert resp.status_code == 200, f"excel upload failed {resp.text}"
    assert resp.json()["file_type"] == "xlsx"
    assert len(resp.json()["preview"]) == 2

def test_rls_policy_exists_for_new_tables():
    """پالیسی‌های جدید باید Fail-Closed و FORCE باشند"""
    # use admin engine to inspect pg_policy (bypass RLS)
    eng = seed_engine
    with eng.connect() as conn:
        for tbl in ["data_sources", "fact_rows"]:
            row = conn.execute(text(f"SELECT pg_get_expr(polqual, polrelid) FROM pg_policy WHERE polname='tenant_isolation' AND polrelid='{tbl}'::regclass")).fetchone()
            # fallback if not found via pg_get_expr, try polqual::text
            if not row or not row[0]:
                row = conn.execute(text(f"SELECT polqual::text FROM pg_policy WHERE polname='tenant_isolation' AND polrelid='{tbl}'::regclass")).fetchone()
            assert row is not None and row[0] is not None, f"policy for {tbl} not found"
            qual = str(row[0])
            assert "IS NULL" not in qual.upper(), f"{tbl} policy should be Fail-Closed, got {qual}"
            assert "current_setting" in qual, f"{tbl} policy missing current_setting"
            force = conn.execute(text(f"SELECT relforcerowsecurity FROM pg_class WHERE relname='{tbl}'")).fetchone()
            assert force and force[0] is True, f"{tbl} should have FORCE RLS"
