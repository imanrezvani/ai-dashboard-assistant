"""
تست integration واقعی RLS روی PostgreSQL

سناریو دقیقا مطابق درخواست:
  الف) دو organization و membership برای هر org می‌سازد
  ب) یک session مستقیم که «مثل باگ اپلیکیشن» عمل می‌کند:
     SET LOCAL app.current_org_id = '<org A>' ست می‌کند، ولی query را بدون WHERE می‌نویسد
     db.query(Membership).all()
  ج) انتظار: فقط membershipهای org A برگردد (نه B) — دیتابیس حتی بدون فیلتر اپ، فیلتر می‌کند
  د) اگر اصلا SET LOCAL زده نشود، همان query باید خالی برگردد (Fail-Closed)

این تست فقط روی PostgreSQL اجرا می‌شود؛ روی SQLite skip می‌شود.
برای اجرا:
  docker compose -f docker-compose.test.yml up -d
  TEST_DATABASE_URL=postgresql+psycopg://tasmim:tasmim_secret@localhost:5433/tasmim_yar_test pytest -v
یا
  TEST_DATABASE_URL=postgresql+psycopg://tasmim_app:tasmim_app_secret@localhost:5432/tasmim_yar pytest -v
"""

import os
import uuid
import pathlib
import sys

API_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# مدل‌ها
from app.core.database import Base
from app.models import Membership, Organization, RoleEnum, User
from app.core.security import hash_password


# ----- کشف URL تست -----
def _make_app_url(base_url: str, user: str, pwd: str) -> str:
    import urllib.parse as up
    p = up.urlparse(base_url)
    netloc = f"{user}:{pwd}@{p.hostname}"
    if p.port:
        netloc += f":{p.port}"
    return up.urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def _candidate_urls():
    # اولویت: env کاربر — اگر TEST روی Neon باشد، ابتدا نسخه tasmim_app همان هاست را امتحان کن
    env_test = os.getenv("TEST_DATABASE_URL")
    env_admin = os.getenv("ADMIN_DATABASE_URL")
    if env_test:
        # اگر TEST خود tasmim_app است، همان را اول بده
        if "tasmim_app" in env_test:
            yield env_test
        # وگرنه نسخه app روی همان هاست را بساز (Neon pooled)
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
    # fallback لوکال (docker-compose)
    yield "postgresql+psycopg://tasmim_app:tasmim_app_secret@localhost:5432/tasmim_yar"
    yield "postgresql+psycopg://tasmim:tasmim_secret@localhost:5432/tasmim_yar"
    yield "postgresql+psycopg://tasmim:tasmim_secret@localhost:5433/tasmim_yar_test"
    yield "postgresql+psycopg://tasmim_app:tasmim_app_secret@localhost:5433/tasmim_yar_test"


def _get_test_engine():
    last_err = None
    for url in _candidate_urls():
        try:
            eng = create_engine(url, pool_pre_ping=True)
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
            print(f"[test_rls_postgres] connected with {url}")
            return eng, url
        except Exception as e:
            last_err = e
            continue
    pytest.skip(f"PostgreSQL در دسترس نیست ({last_err}); برای اجرای این تست: docker compose -f docker-compose.test.yml up -d یا docker compose up -d", allow_module_level=True)
    raise RuntimeError(last_err)


engine, used_url = _get_test_engine()
is_app_user = "tasmim_app" in used_url

# اطمینان از تمیز بودن
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

# اعطای دسترسی به tasmim_app اگر با superuser وصل شدیم
if not is_app_user:
    try:
        with engine.begin() as conn:
            conn.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO tasmim_app;"))
            conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO tasmim_app;"))
    except Exception as e:
        print(f"[grant warning] {e}")

# ----- فعال‌سازی RLS Fail-Closed + FORCE (دقیقا مثل app/main.py) -----
with engine.begin() as conn:
    for table in ["memberships"]:
        conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;"))
        conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;"))
        conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table};"))
        conn.execute(text(f"""
            CREATE POLICY tenant_isolation ON {table}
            FOR ALL
            USING (organization_id::text = current_setting('app.current_org_id', true))
            WITH CHECK (organization_id::text = current_setting('app.current_org_id', true));
        """))

# اگر با app_user وصل نشدیم، برای تست RLS باید اتصال دوم با app_user بسازیم
# چون superuser حتی با FORCE از RLS معاف است (BYPASSRLS)
if not is_app_user:
    # سعی کن با app_user وصل شوی؛ اگر نشد، تست را با هشدار روی superuser اجرا کن
    # اما نتیجه روی superuser قابل اعتماد نیست (همه ردیف‌ها برمی‌گردد)
    app_url = "postgresql+psycopg://tasmim_app:tasmim_app_secret@localhost:5432/tasmim_yar"
    # اگر تست روی 5433 است، app_url متناظر
    if "5433" in used_url:
        app_url = "postgresql+psycopg://tasmim_app:tasmim_app_secret@localhost:5433/tasmim_yar_test"
    try:
        app_engine = create_engine(app_url, pool_pre_ping=True)
        with app_engine.connect() as c:
            c.execute(text("SELECT 1"))
        # سوئیچ به app_engine برای ادامه
        engine = app_engine
        is_app_user = True
        print(f"[test_rls_postgres] switched to app user engine {app_url}")
    except Exception as e:
        print(f"[warning] cannot connect as tasmim_app ({e}); RLS tests on superuser will show BYPASSRLS behavior")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# ----- داده اولیه: دو org + دو membership -----
def _seed():
    # پاکسازی قبلی
    with SessionLocal() as db:
        db.execute(text("DELETE FROM memberships;"))
        db.execute(text("DELETE FROM organizations;"))
        db.execute(text("DELETE FROM users;"))
        db.commit()
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()
    user_a_id = uuid.uuid4()
    user_b_id = uuid.uuid4()
    with SessionLocal() as db:
        # برای seed باید RLS را دور زد — seed را با role superuser یا با SET LOCAL مناسب انجام می‌دهیم
        # ساده‌ترین: موقتا policy را با superuser دور بزنیم؛ چون seed با همین engine (که ممکن است app_user باشد)
        # نیاز به SET LOCAL دارد. برای هر org جداگانه SET LOCAL می‌کنیم.
        # راه حل: مستقیم با connection superuser insert کنیم اگر app_user است،
        # ولی برای سادگی: قبل از seed، RLS را غیرفعال موقت کنیم و بعد دوباره فعال کنیم — یا با BypassInsert
        # ساده‌ترین: از engine اصلی (که ممکن است app_user باشد) با دو تراکنش جدا که هر کدام org خودش را ست کرده
        pass

    # برای اطمینان seed را با اتصال superuser انجام می‌دهیم (اگر app_user هستیم، یک engine دوم superuser می‌سازیم)
    seed_urls = [
        "postgresql+psycopg://tasmim:tasmim_secret@localhost:5432/tasmim_yar",
        "postgresql+psycopg://tasmim:tasmim_secret@localhost:5433/tasmim_yar_test",
    ]
    # سعی کن seed engine پیدا کنی که superuser باشد و به همان DB وصل شود
    seed_engine = None
    for u in seed_urls:
        if ("5432" in used_url and "5432" in u) or ("5433" in used_url and "5433" in u):
            try:
                se = create_engine(u, pool_pre_ping=True)
                with se.connect() as c:
                    c.execute(text("SELECT 1"))
                seed_engine = se
                break
            except Exception:
                continue
    if seed_engine is None:
        seed_engine = engine  # fallback: با همان engine seed کن (نیاز به SET LOCAL)

    # اگر seed_engine همان app_user است، برای insert باید SET LOCAL را ست کنیم یا RLS را موقتا bypass کنیم
    # روش: برای هر org یک تراکنش با SET LOCAL همان org
    def insert_with_context(org_id, user_id, email, full_name, slug, name):
        with seed_engine.begin() as conn:
            # ست کردن context برای اینکه WITH CHECK اجازه insert بدهد
            # current_setting خالی بود، پس insert بدون context fail می‌شود؛ باید قبلش ست کنیم
            try:
                conn.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(org_id)})
            except Exception:
                pass
            # users و organizations بدون RLS هستند — مستقیم insert
            conn.execute(text("INSERT INTO users (id, email, full_name, hashed_password) VALUES (:id, :email, :full_name, :hp)"),
                         {"id": str(user_id), "email": email, "full_name": full_name, "hp": hash_password("TestPass123")})
            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
                         {"id": str(org_id), "name": name, "slug": slug})
            # memberships تحت RLS است — چون context ست شده، WITH CHECK پاس می‌شود
            conn.execute(text("INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:id, :uid, :oid, :role)"),
                         {"id": str(uuid.uuid4()), "uid": str(user_id), "oid": str(org_id), "role": "owner"})

    # اگر seed_engine superuser است، می‌توان بدون SET LOCAL هم insert کرد چون BYPASSRLS دارد؛
    # ولی برای یکنواختی، همان روال بالا را نگه می‌داریم
    # ابتدا چک کنیم superuser است یا نه
    is_seed_superuser = False
    try:
        with seed_engine.connect() as c:
            r = c.execute(text("SELECT usesuper FROM pg_user WHERE usename = current_user")).fetchone()
            if r and r[0]:
                is_seed_superuser = True
    except Exception:
        pass

    if is_seed_superuser:
        # superuser می‌تواند بدون SET LOCAL insert کند (BYPASSRLS)، پس مستقیم
        with seed_engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, email, full_name, hashed_password) VALUES (:id, :email, :full_name, :hp)"),
                         {"id": str(user_a_id), "email": "alice@org-a.test", "full_name": "Alice A", "hp": hash_password("TestPass123")})
            conn.execute(text("INSERT INTO users (id, email, full_name, hashed_password) VALUES (:id, :email, :full_name, :hp)"),
                         {"id": str(user_b_id), "email": "bob@org-b.test", "full_name": "Bob B", "hp": hash_password("TestPass123")})
            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
                         {"id": str(org_a_id), "name": "Organization A", "slug": "org-a"})
            conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
                         {"id": str(org_b_id), "name": "Organization B", "slug": "org-b"})
            conn.execute(text("INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:id, :uid, :oid, :role)"),
                         {"id": str(uuid.uuid4()), "uid": str(user_a_id), "oid": str(org_a_id), "role": "owner"})
            conn.execute(text("INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:id, :uid, :oid, :role)"),
                         {"id": str(uuid.uuid4()), "uid": str(user_b_id), "oid": str(org_b_id), "role": "owner"})
    else:
        insert_with_context(org_a_id, user_a_id, "alice@org-a.test", "Alice A", "org-a", "Organization A")
        insert_with_context(org_b_id, user_b_id, "bob@org-b.test", "Bob B", "org-b", "Organization B")

    return org_a_id, org_b_id

ORG_A_ID, ORG_B_ID = _seed()


def test_rls_bypass_attribute():
    """کاربر اپ نباید BYPASSRLS داشته باشد (وگرنه FORCE هم بی‌اثر است برای superuser)."""
    with engine.connect() as conn:
        row = conn.execute(text("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = current_user")).fetchone()
        assert row is not None, "role یافت نشد"
        bypass, issuper = row[0], row[1]
        # اگر با superuser وصل شدیم، این تست هشدار می‌دهد ولی fail نمی‌کند
        if is_app_user:
            assert bypass is False, "app role نباید BYPASSRLS داشته باشد"
            assert issuper is False, "app role نباید superuser باشد"
        else:
            pytest.skip(f"connected as superuser (bypass={bypass}, super={issuper}); برای تست واقعی با tasmim_app وصل شوید")


def test_rls_policy_is_fail_closed():
    """پالیسی باید Fail-Closed باشد (بدون IS NULL)."""
    with engine.connect() as conn:
        # روی Neon/Postgres 18، polqual as text برمی‌گردد به صورت OPEXPR داخلی؛
        # برای گرفتن SQL واقعی باید pg_get_expr استفاده کرد
        row = conn.execute(text("""
            SELECT pg_get_expr(polqual, polrelid) AS qual,
                   pg_get_expr(polwithcheck, polrelid) AS with_check
            FROM pg_policy
            WHERE polname = 'tenant_isolation'
        """)).fetchone()
        if row is None or row[0] is None:
            # fallback برای نسخه‌های قدیمی که pg_get_expr null برمی‌گرداند
            row2 = conn.execute(text("""
                SELECT polqual::text, polwithcheck::text FROM pg_policy
                WHERE polname = 'tenant_isolation'
            """)).fetchone()
            assert row2 is not None, "policy tenant_isolation یافت نشد"
            qual = str(row2[0]) if row2[0] else ""
            with_check = str(row2[1]) if row2[1] else ""
        else:
            qual = str(row[0]) if row[0] else ""
            with_check = str(row[1]) if row[1] else ""
        # نباید IS NULL fallback داشته باشد
        assert "IS NULL" not in qual.upper(), f"policy باید Fail-Closed باشد، ولی IS NULL دارد: {qual}"
        assert "current_setting" in qual, f"policy باید از current_setting استفاده کند: {qual}"
        # باید FORCE باشد
        force_row = conn.execute(text("""
            SELECT relforcerowsecurity FROM pg_class WHERE relname = 'memberships'
        """)).fetchone()
        assert force_row is not None and force_row[0] is True, "memberships باید FORCE ROW LEVEL SECURITY باشد"


def test_rls_without_where_returns_only_tenant_rows():
    """حتی اگر اپ WHERE را فراموش کند (query بدون فیلتر)، DB فقط ردیف‌های tenant ست‌شده را برمی‌گرداند."""
    if not is_app_user:
        pytest.skip("این تست فقط با role غیر-superuser معتبر است (superuser Bypass می‌کند)")
    with SessionLocal() as db:
        # ست کردن tenant برای org A
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(ORG_A_ID)})
        # عمدا بدون WHERE — شبیه باگ اپ
        rows = db.query(Membership).all()
        # باید فقط ۱ ردیف (org A) برگردد، نه ۲
        assert len(rows) == 1, f"expected 1 row for org A, got {len(rows)}: {[r.organization_id for r in rows]}"
        assert str(rows[0].organization_id) == str(ORG_A_ID)
        # برای اطمینان، همین را برای org B هم چک کن
        db.rollback()  # پایان تراکنش قبلی (SET LOCAL پاک می‌شود)
        db.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(ORG_B_ID)})
        rows_b = db.query(Membership).all()
        assert len(rows_b) == 1
        assert str(rows_b[0].organization_id) == str(ORG_B_ID)
        db.rollback()


def test_rls_without_context_returns_empty_fail_closed():
    """اگر اصلا SET LOCAL زده نشود، query بدون فیلتر باید خالی برگردد (نه همه ردیف‌ها)."""
    if not is_app_user:
        pytest.skip("این تست فقط با role غیر-superuser معتبر است")
    with SessionLocal() as db:
        # هیچ set_config صدا نمی‌زنیم — context خالی
        # برای اطمینان، مقدار فعلی را پاک می‌کنیم
        try:
            db.execute(text("SELECT set_config('app.current_org_id', '', true)"))
        except Exception:
            pass
        rows = db.query(Membership).all()
        # Fail-Closed: باید ۰ ردیف
        assert len(rows) == 0, f"Fail-Closed: بدون context باید 0 ردیف برگردد، ولی {len(rows)} برگشت: {[r.organization_id for r in rows]}"
        db.rollback()


def test_rls_insert_without_context_fails():
    """insert بدون context باید fail شود (WITH CHECK)."""
    if not is_app_user:
        pytest.skip("فقط با app_user")
    with SessionLocal() as db:
        try:
            db.execute(text("SELECT set_config('app.current_org_id', '', true)"))
        except Exception:
            pass
        # تلاش برای insert membership بدون context — باید خطای policy بدهد
        new_id = uuid.uuid4()
        fake_user = uuid.uuid4()
        # یک user موقت می‌سازیم (users بدون RLS)
        db.execute(text("INSERT INTO users (id, email, full_name, hashed_password) VALUES (:id, :email, :fn, :hp)"),
                   {"id": str(fake_user), "email": f"tmp-{new_id}@test.local", "fn": "Tmp", "hp": hash_password("x")})
        db.flush()
        with pytest.raises(Exception):
            db.execute(text("INSERT INTO memberships (id, user_id, organization_id, role) VALUES (:id, :uid, :oid, :role)"),
                       {"id": str(new_id), "uid": str(fake_user), "oid": str(ORG_A_ID), "role": "viewer"})
            db.flush()
        db.rollback()
