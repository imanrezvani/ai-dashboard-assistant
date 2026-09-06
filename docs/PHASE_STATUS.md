# PHASE_STATUS — وضعیت فازهای پروژه تصمیم‌یار

> **منبع حقیقت این سند:** فقط کد tracked ریپو (routers/models/pages/tests)، `README.md` و تاریخچه git (فقط ۱ commit).
> هیچ فازی «حدس» زده نشده؛ هر مورد با فایل مرجعش قابل راستی‌آزمایی است.

## کارهای انجام‌شده (تأییدشده در کد)

**فاز ۱ — چندمستاجری + احراز هویت + منابع داده** (طبق «مرحله ۱» در README):

- Backend: FastAPI + SQLAlchemy 2 + PostgreSQL — routers: `auth`, `organizations`, `data_sources` (`apps/api/app/routers/`)
- احراز هویت JWT (python-jose + bcrypt)، ثبت‌نام/ورود؛ نقش‌ها: Owner, Admin, Manager, Analyst, Viewer (`app/models/membership.py`)
- RLS روی `memberships` / `data_sources` / `fact_rows`: ENABLE + **FORCE** + پالیسی Fail-Closed (بدون `IS NULL` fallback)؛ اپ با role غیر-superuser (`tasmim_app` NOBYPASSRLS) وصل می‌شود (`app/main.py`, `apps/api/init-db/01-app-user.sh`, `docker-compose.yml`)
- Middleware tenant: `set_config('app.current_org_id' / 'app.current_user_id')` + `require_membership` / `require_role` (`app/middleware/tenant.py`)
- منابع داده: آپلود CSV/Excel، پیش‌نمایش، Map → درج در `fact_rows` با `organization_id`
- Frontend: Next.js 14 + Tailwind + shadcn/ui، صفحات فارسی RTL: `/`, `/login`, `/register`, `/dashboard`, `/data-sources`, `/settings` (`apps/web/app/`)
- تست‌ها: `test_tenant_isolation.py` (API-level)؛ `test_rls_postgres.py` و `test_data_sources.py` (integration روی PostgreSQL واقعی — بدون DB به‌صورت module-level skip می‌شوند)
- سخت‌گیری production: نبود `JWT_SECRET` / `TASMIM_APP_DB_PASSWORD` / `DATABASE_URL` در production خطای صریح `RuntimeError` می‌دهد (commit `71500b6` + رفع باگ مقدار خالی DATABASE_URL در `app/core/config.py`)

## فاز فعلی

**فاز ۲.۰ — baseline Alembic — تکمیل‌شده:**
- `apps/api/alembic.ini` + `apps/api/alembic/env.py` (URL از همان زنجیره settings اپ؛ offline `--sql`، CLI و حالت in-process) + `script.py.mako`
- baseline migration: `alembic/versions/20260906_0001_baseline.py` — دقیقاً schema فاز ۱ (۵ جدول: organizations, users, memberships, data_sources, fact_rows) بدون هیچ جدول جدید و بدون پالیسی RLS (طبق محدودیت؛ RLS همچنان در runtime در `app/main.py` اعمال می‌شود)
- `app/core/migrations.py`: اجرای migration در startup با `pg_advisory_xact_lock` (جلوگیری از migrate همزمان چند instance)؛ در dialect غیر-PostgreSQL (تست‌های SQLite) no-op
- `Base.metadata.create_all` از startup حذف شد؛ منطق: دیتابیس تازه → `upgrade head`، دیتابیس موجود فاز ۱ (بدون `alembic_version`) → `stamp head`
- رفتار API تغییر نکرده؛ هر ۷ تست API همان پاس‌های قبلی را دارند

## گام‌های بعدی (طبق «مرحله بعد» در README)

فاز ۲ (بقیه): ۱) ماندگاری فایل آپلودی + schema ستون‌ها (حذف `PENDING_UPLOADS` حافظه‌ای) ۲) اتصال دیتابیس خارجی ۳) موتور KPI ۴) کاتالوگ زمینه (آماده‌سازی دستیار AI)

## بدهی فنی شناخته‌شده

- **مایگریشن دیتابیس:** تا فاز ۱ schema فقط با `create_all` ساخته می‌شد — از فاز ۲.۰ با Alembic مدیریت می‌شود (baseline: `0001_baseline`). تغییرات schema آینده فقط با migration جدید.
- **CI وجود ندارد** (پوشه `.github/` نیست)؛ تست‌ها فقط به‌صورت محلی اجرا می‌شوند.
- **پیکربندی pytest وجود ندارد** (`conftest.py`/`pytest.ini`/`pyproject.toml` نیست)؛ path و env توسط خود فایل‌های تست ست می‌شود.
- تست‌های integration بدون PostgreSQL در دسترس skip می‌شوند؛ اجرای کامل نیاز به `docker compose -f docker-compose.test.yml up -d` دارد.
- مقدارهای fallback رمزهای dev در `config.py` فقط برای dev هستند (production با RuntimeError متوقف می‌شود) — حذف کامل آن‌ها بعد از استقرار مدیریت secretها.
- این سند جدید است: `docs/PHASE_STATUS.md` پیش‌تر در تاریخچه git وجود نداشت.
