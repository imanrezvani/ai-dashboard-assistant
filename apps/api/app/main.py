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
    # فعال‌سازی RLS
    try:
        with engine.begin() as conn:
            # فعال‌سازی RLS روی جداول داده‌محور
            for table in ["memberships"]:
                conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;"))
                # پالیسی permissive به عنوان fallback (اعتبارسنجی اصلی در اپلیکیشن است؛ RLS لایه دوم)
                # این پالیسی اجازه می‌دهد سرویس با app role همه ردیف‌ها را ببیند ولی اگر اتصال
                # با current_setting('app.current_org_id') استفاده شود، فیلتر اعمال می‌شود
                conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table};"))
                conn.execute(text(f"""
                    CREATE POLICY tenant_isolation ON {table}
                    USING (
                        current_setting('app.current_org_id', true) IS NULL
                        OR organization_id::text = current_setting('app.current_org_id', true)
                    );
                """))
            # organizations و users سراسری هستند (بدون RLS) اما memberships محافظت می‌شود
    except Exception as e:
        print(f"[RLS] setup warning: {e}")


@app.get("/health")
def health():
    return {"status": "ok"}
