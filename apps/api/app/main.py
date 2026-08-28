from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.database import Base, engine
from app.models import *  # noqa: F401,F403 - ثبت مدل‌ها برای create_all
from app.routers import auth, organizations

app = FastAPI(title="تصمیم‌یار API", version="0.1.0")

# CORS
origins = [o.strip() for o in settings.CORS_ORIGINS.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(organizations.router)


@app.on_event("startup")
def on_startup():
    # ایجاد جداول اگر وجود نداشته باشند
    Base.metadata.create_all(bind=engine)
    # فعال‌سازی RLS با FORCE (بدون FORCE، owner دیتابیس از policy معاف است)
    # توجه: superuser همیشه BYPASSRLS دارد و حتی FORCE را دور می‌زند؛
    # به همین دلیل اپ باید با role غیر-superuser (tasmim_app NOBYPASSRLS) وصل شود —
    # ر.ک. apps/api/init-db/01-app-user.sql و docker-compose.yml
    try:
        with engine.begin() as conn:
            # اعطای دسترسی به role اپ (اگر وجود داشته باشد؛ در تست SQLite نادیده گرفته می‌شود)
            try:
                conn.execute(text("GRANT CONNECT ON DATABASE tasmim_yar TO tasmim_app;"))
            except Exception:
                pass
            try:
                conn.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO tasmim_app;"))
                conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO tasmim_app;"))
            except Exception:
                pass
            # فعال‌سازی RLS روی جداول داده‌محور
            for table in ["memberships"]:
                conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;"))
                conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;"))
                # پالیسی Fail-Closed: اگر app.current_org_id ست نشده باشد،
                # current_setting(..., true) رشته خالی '' برمی‌گرداند و مقایسه
                # organization_id::text = '' همیشه false است → هیچ ردیفی برنمی‌گردد.
                # این تضمین می‌کند حتی اگر اپ فیلتر را فراموش کند، DB جلوی نشت را می‌گیرد.
                conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table};"))
                conn.execute(text(f"""
                    CREATE POLICY tenant_isolation ON {table}
                    FOR ALL
                    USING (organization_id::text = current_setting('app.current_org_id', true))
                    WITH CHECK (organization_id::text = current_setting('app.current_org_id', true));
                """))
            # organizations و users سراسری هستند (بدون RLS) اما memberships محافظت می‌شود
    except Exception as e:
        print(f"[RLS] setup warning: {e}")


@app.get("/health")
def health():
    return {"status": "ok"}
