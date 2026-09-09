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
- `Base.metadata.create_all` از startup حذف شد؛ منطق: دیتابیس تازه → `upgrade head`، دیتابیس موجود فاز ۱ (بدون `alembic_version`) → `stamp 0001_baseline` سپس `upgrade head` (تا migrationهای بعد از baseline از قلم نیفتند)
- رفتار API تغییر نکرده؛ هر ۷ تست API همان پاس‌های قبلی را دارند

**فاز ۲.۱ — ماندگاری فایل آپلودی + متادیتای ستون‌ها (حذف PENDING_UPLOADS) — تکمیل‌شده (گیت PostgreSQL گذرانده شد):**
- انتزاع `FileStorage` (`app/core/storage.py`: save/load/delete، همه tenant-safe) + پشتیبان PostgreSQL bytea (`app/core/storage_postgres.py`)
- مدل‌های جدید: `DataSourceFile` (`data_source_files` — بایت‌های فایل، ۱:۱ با data_sources) و `DataSourceColumn` (`data_source_columns` — نام/ترتیب/dtype/نقش map شده)
- migration `alembic/versions/20260906_0002_persist_uploads.py` (زنجیره: 0001_baseline → 0002_persist_uploads) — بدون تغییر جداول موجود و بدون پالیسی RLS (طبق قاعده؛ RLS در runtime)
- RLS سه‌لایه برای جدول‌های جدید: ستون اجباری `organization_id` (FK CASCADE) + پالیسی Fail-Closed `ENABLE+FORCE` در `app/main.py` + فیلتر `organization_id` در همه کوئری‌های storage
- `PENDING_UPLOADS` کاملاً حذف شد (فقط اشاره مستنداتی به‌عنوان «جایگزین‌شده» باقی است)؛ map پس از restart هم کار می‌کند (بازیابی از bytea)
- پوشش تست فاز ۲.۱ تکمیل شد: assertionهای persistence (بایت‌های فایل/size/content_type در data_source_files)، متادیتای ستون‌ها (نام/ترتیب/dtype در data_source_columns)، mapped_role پس از map، اثبات خواندن map از نسخه ماندگار (با تغییر bytea در DB بین upload و map، خروجی map باید از نسخه ماندگار بیاید — نه حافظه پروسه)، tenant isolation دو جدول جدید (DB-level با org B + FileNotFound از لایه storage)، اضافه‌شدن دو جدول جدید به RLS setup محیط تست، و assert ENABLE + FORCE + Fail-Closed + org-scoped برای هر چهار جدول داده‌ای. تست unit لایه storage روی SQLite (`test_storage_unit.py`: round-trip بایت، فیلتر org، FileNotFound، delete) بدون نیاز به DB اجرا می‌شود — رفتار RLS fake نمی‌شود و فقط در تست‌های PostgreSQL واقعی آزموده می‌شود
- وضعیت اجرا — گیت PostgreSQL واقعی گذرانده شد (PostgreSQL 14، دیتابیس `tasmim_yar_test` روی پورت 5433 با role اپ `tasmim_app` NOBYPASSRLS/NOSUPERUSER): **۲۳ passed، 0 failed، 0 skipped** در یک session — هر دو ماژول integration با هم اجرا شدند (persistence بایت‌ها و متادیتا، round-trip map از نسخه ماندگار، mapped_role، tenant-isolation جدول‌های جدید، RLS ENABLE+FORCE+Fail-Closed برای چهار جدول، NOBYPASSRLS و WITH CHECK در runtime)؛ `alembic upgrade head` روی دیتابیس خالی تا `0003` اعمال شد، `alembic current` = `0003_align_created_at_nullable`، دقیقاً یک head، و `alembic check` = «No new upgrade operations detected» (قبل و بعد از اجرای تست‌ها)
- دو عیب واقعی در گیت کشف و رفع شد: ۱) reset مخرب schema در import-time `test_rls_postgres.py` به‌همراه clobber شدن `dependency_overrides[get_db]` در import-time `test_tenant_isolation.py` که اجرای هم‌زمان دو ماژول integration در یک session را می‌شکست — با حذف drop_all، cleanup فقط ردیف‌های خود ماژول و fixture خود-ترمیم override رفع شد ۲) drift nullable سه ستون `created_at` (organizations/users/memberships) بین baseline و مدل‌ها — با migration جدید `0003_align_created_at_nullable` هم‌تراز شد (baseline دست‌نخورده؛ `alembic check` اکنون پاک است)

**فاز ۲.۲ — اتصالات دیتابیس خارجی — در حال اجرا (گام ۱: foundation تکمیل شد):**
- مدل `DatabaseConnection` (`database_connections` — `encrypted_password` فقط-ciphertext، `enabled`/`status`/`last_checked_at`، یونیک name per-org) + migration `0004_database_connections` روی زنجیره `0003` (تک‌head، بدون create_all)
- سرویس رمزنگاری `app/core/credentials.py` (Fernet از `cryptography==50.0.1`) با `ENCRYPTION_KEY` در settings — نبود کلید در production خطای صریح می‌دهد؛ کلید Fernet از sha256(secret) مشتق می‌شود
- RLS runtime در `app/main.py`: ENABLE + FORCE + Fail-Closed برای `database_connections` (همان الگوی فاز ۲.۱)
- تست‌ها: ۹ تست unit جدید (`test_db_connection_unit.py`: round-trip/tamper/کلید اشتباه/الزام کلید در production/defaults/یونیک per-org/نبودِ plaintext پس از persist/فیلتر org/cascade) + گسترش پوشش integration (`test_data_sources.py`: RLS setup + assert ENABLE/FORCE/Fail-Closed/org-scoped برای جدول جدید + ایزولاسیون tenant ردیف اتصال)
- گیت: **۳۲ passed، 0 failed، 0 skipped** روی PostgreSQL واقعی؛ `alembic upgrade head` روی دیتابیس خالی تا `0004`؛ `alembic current` = `0004_database_connections`؛ `alembic check` پاک (قبل و بعد از تست‌ها)

**فاز ۲.۲ — گام ۲: connector PostgreSQL — تکمیل‌شده:**
- انتزاع `DatabaseConnector` (`app/connectors/base.py`): ABC با `test_connection` / `discover_tables` / `sample_rows` / `fetch_dataframe` + `ConnectorError`/`ConnectorTimeout`/`ConnectorReadOnlyViolation` sanitized + `ConnectionCheck`/`TableInfo` (بدون هیچ داده اعتبارنامه) + factory `get_connector` (فقط postgresql؛ سایر موتورها → ConnectorError)
- پیاده‌سازی PostgreSQL (`app/connectors/postgres.py`) — مرز امنیتی:
  - فقط SELECT/catalog؛ هیچ متدی متن SQL نمی‌پذیرد (arbitrary SQL وجود ندارد)
  - شناسه جدول/schema فقط با `psycopg.sql.Identifier` — `table` فقط پس از تطبیق با خروجی `discover_tables` پذیرفته می‌شود (نام تزریق‌شده → «table not found»)
  - نشست read-only (`default_transaction_read_only=on`) + `statement_timeout` + `connect_timeout`؛ بستن connection همیشه با rollback (هیچ commit ای)
  - سقف ردیف: `MAX_SAMPLE_ROWS=1000` / `MAX_FETCH_ROWS=100000` — limit ورودی clamp می‌شود
  - رمز فقط داخل `_connect` و فقط پس از RBAC+RLS decrypt می‌شود (kwargs-based، هیچ conninfo-string حاوی رمز ساخته نمی‌شود)؛ خطاها sanitized (بدون رمز/DSN/جزئیات درایور)
- تست‌ها: ۹ تست unit (`test_connector_unit.py`: قرارداد ABC، clamping حدی، شناسه تزریق‌شده، credential non-disclosure، factory) + ۱۹ تست integration روی PostgreSQL واقعی (`test_connector_postgres.py`: اتصال موفق/رمز غلط sanitized/host غیرقابل‌دسترس و blackhole با connect_timeout محدود/discovery/sample/DataFrame/سقف ردیف/statement_timeout با قفل واقعی ACCESS EXCLUSIVE/خواندن-فقط در سطح session با INSERT رد شده/نبود plaintext در state و خطاها/RLS cross-org و fail-closed)
- گیت: **۶۳ passed، 0 failed، 0 skipped** در یک session (۳۲ قبلی + ۱۹ connector + ۱۲ unit)؛ بدون migration جدید (گام ۲ هیچ تغییر schema لازم نداشت)؛ `alembic current` = `0004_database_connections`، تک‌head، `alembic check` پاک

**فاز ۲.۲ — گام ۳: API endpoints — تکمیل‌شده:**
- راستر `app/routers/database_connections.py` با هر ۷ اندپوینت طرح (§6): POST create (admin+، password فقط-ورودی و encrypt)، GET list/get (manager+)، DELETE (admin+)، POST test (admin+ — اجرای واقعی test_connection و ثبت status/last_checked_at/last_error sanitized)، GET tables (manager+ — discover_tables)، GET tables/{table}/sample (analyst+ — sample_rows با limit≤1000)
- ماتریس نقش‌ها با `require_role` موجود؛ cross-org id → **404** (نه 403) با RLS + فیلتر صریح organization_id؛ اتصال disabled → 400 روی test/tables/sample؛ duplicate name → 409؛ بدون PUT/PATCH (rotate = delete + recreate)
- اسکیماها (`app/schemas/database_connection.py`): پاسخ‌ها `has_stored_credentials` دارند و هیچ رمز/hint/DSN برنمی‌گردانند؛ `engine` فقط postgresql؛ `ssl_mode` whitelist
- تست‌ها: ۱۵ تست integration جدید (`test_db_connections_api.py`) روی PostgreSQL واقعی با scratch external: ماتریس نقش‌ها (viewer/analyst/manager/admin)، write-only بودن password (create/get/list/DB)، cross-org 404 روی همه اندپوینت‌ها، duplicate، flow واقعی test/tables/sample، خطای sanitized رمز غلط، clamping/اعتبارسنجی limit، جدول ناموجود → 502، disabled → 400، lifecycle کامل create→test→tables→sample→delete
- گیت: **۷۸ passed، 0 failed، 0 skipped** در یک session (۶۳ قبلی + ۱۵ API)؛ بدون migration جدید؛ `alembic current` = `0004_database_connections`، تک‌head، `alembic check` پاک
- باقی‌مانده فاز ۲.۲ (طبق `docs/PHASE2_PLAN.md`): import bridge — شروع نشده

## گام‌های بعدی

فاز ۲ (بقیه): ۱) اتصال دیتابیس خارجی ۲) موتور KPI ۳) کاتالوگ زمینه (آماده‌سازی دستیار AI)

طرح تفصیلی و منبع حقیقت پیاده‌سازی فاز ۲.۲ (اتصال دیتابیس خارجی): **`docs/PHASE2_PLAN.md`** — گام‌های ۱ (foundation)، ۲ (connector PostgreSQL) و ۳ (API endpoints) تکمیل شدند؛ گام بعدی: import bridge (طبق طرح).

## بدهی فنی شناخته‌شده

- **مایگریشن دیتابیس:** تا فاز ۱ schema فقط با `create_all` ساخته می‌شد — از فاز ۲.۰ با Alembic مدیریت می‌شود (baseline: `0001_baseline`). تغییرات schema آینده فقط با migration جدید.
- **CI وجود ندارد** (پوشه `.github/` نیست)؛ تست‌ها فقط به‌صورت محلی اجرا می‌شوند.
- **پیکربندی pytest وجود ندارد** (`conftest.py`/`pytest.ini`/`pyproject.toml` نیست)؛ path و env توسط خود فایل‌های تست ست می‌شود.
- تست‌های integration بدون PostgreSQL در دسترس skip می‌شوند؛ اجرای کامل نیاز به `docker compose -f docker-compose.test.yml up -d` دارد.
- مقدارهای fallback رمزهای dev در `config.py` فقط برای dev هستند (production با RuntimeError متوقف می‌شود) — حذف کامل آن‌ها بعد از استقرار مدیریت secretها.
- این سند جدید است: `docs/PHASE_STATUS.md` پیش‌تر در تاریخچه git وجود نداشت.
