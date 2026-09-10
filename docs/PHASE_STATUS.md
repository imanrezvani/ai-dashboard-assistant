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
**فاز ۲.۲ — گام ۴: import bridge — تکمیل‌شده (فاز ۲.۲ کامل شد):**
- فقط یک ستون nullable به مدل DataSource اضافه شد: `database_connection_id` (FK به `database_connections.id` با `ON DELETE SET NULL` — حذف اتصال، منابع import شده و fact_rows آن‌ها را دست‌نخورده می‌گذارد؛ فقط خط منشأ NULL می‌شود) + ایندکس؛ `DataSourceOut` فیلد اختیاری دارد
- migration `0005_data_source_provenance` روی زنجیره `0004` (تک‌head؛ بدون RLS در migration طبق قاعده) — درس `0003`: ستون مدل `index=True` دارد تا `alembic check` پاک بماند (drift index در گیت کشف و با هم‌ترازسازی مدل رفع شد، migration دست‌نخورده)
- توابع مشترک ingestion بدون تغییر رفتار به `app/services/ingest.py` استخراج شدند (parse/persist_columns/load_dataframe/preview_records) — `data_sources.py` فقط delegate می‌کند؛ آپلود CSV/Excel همان پیام‌ها و کدهای قبلی را دارد
- اندپوینت جدید: `POST /database-connections/{id}/import` (فقط admin+، §6/§7 طرح) — فقط پارامترهای ساختاریافته (`table`/`limit`/`name`)، بدون هیچ SQL خام؛ تنها کاری که می‌کند جایگزینی منبع DataFrame است (فایل آپلودی → جدول PostgreSQL خارجی از طریق connector موجود): DataSource با `file_type="postgres"` + `status="pending"` + ذخیره CSV سریال‌شده در `data_source_files` از طریق همان FileStorage فاز ۲.۱ + `persist_columns` — map کردن همان `POST /data-sources/{id}/map` موجود می‌ماند → fact_rows؛ پیاده‌سازی موازی ingestion وجود ندارد
- تراکنش: واکشی خارجی قبل از هر INSERT اپ؛ خطا پس از ساخت DataSource → rollback کامل (بدون DataSource گمراه‌کننده ناقص)؛ جدول خالی → 400؛ جدول ناموجود/تزریق شناسه → 502 sanitized («table not found»)؛ اتصال ناموفق → 502؛ اتصال disabled → 400؛ cross-org → 404؛ درخواست ناقص/سقف اسکیما (limit > 1M) → 422؛ limit بزرگ‌تر از MAX_FETCH_ROWS در connector clamp می‌شود
- تست‌ها: ۱۷ تست integration جدید (`tests/test_import_bridge.py`) روی PostgreSQL واقعی با scratch external: import موفق + round-trip کامل (DataSource ماندگار با provenance، بایت‌های CSV در data_source_files، متادیتای ستون‌ها با dtype/position، map موجود → ۳ fact_rows با measure/date/label درست و organization_id درست، DataSourceOut نشان‌دهنده provenance)، import دوباره → DataSource مستقل، لایه‌بندی سقف (422 اسکیما + clamp connector)، tenant isolation (cross-org import → 404 و هیچ ردیفی ساخته نمی‌شود؛ org B منبع/فایل/map را نمی‌بیند)، امنیت (رمز plaintext در هیچ پاسخ/خطایی نیست؛ شناسه‌های تزریقی → 502 sanitized بدون اجرا)، رفتار خطا (disabled/رمز غلط/جدول ناموجود/جدول خالی/host غیرقابل‌دسترس با connect_timeout محدود/درخواست ناقص)، provenance (حذف اتصال → SET NULL و DataSource/status/fact_rows سالم و map مجدد کار می‌کند)، schema migration (nullable + FK SET NULL + index) و بدون رگرسیون آپلود (provenance=NULL)
- گیت نهایی: **۹۵ passed، 0 failed، 0 skipped** در یک session روی PostgreSQL واقعی (۷۸ قبلی + ۱۷ جدید)؛ زنجیره کامل روی دیتابیس خالی: ۵/۵ migration اعمال شد، `current` = `0005_data_source_provenance` (تک‌head)، `alembic check` پاک قبل و بعد از تست‌ها؛ role اپ همچنان `NOSUPERUSER`/`NOBYPASSRLS`
- **معیارهای پذیرش §10 طرح همگی سبز شدند — فاز ۲.۲ کامل است.**

## گام‌های بعدی

فاز ۲: ۱) اتصال دیتابیس خارجی — **تکمیل** ۲) موتور KPI — **تکمیل** (`docs/PHASE3_KPI_PLAN.md`) ۳) کاتالوگ زمینه (آماده‌سازی دستیار AI) — **تکمیل** (`docs/PHASE4_CONTEXT_CATALOG_PLAN.md`)

طرح تفصیلی و منبع حقیقت پیاده‌سازی فاز ۲.۲ (اتصال دیتابیس خارجی): **`docs/PHASE2_PLAN.md`** — هر ۴ گام (foundation، connector PostgreSQL، API endpoints، import bridge) تکمیل و گیت شدند؛ **فاز ۲.۲ کامل است.**

**فاز ۲.۳ — موتور KPI — برنامه‌ریزی شد (شروع پیاده‌سازی نشده):**
- سند مرجع: **`docs/PHASE3_KPI_PLAN.md`** — طرح پیاده‌سازی بر پایه شواهد واقعی ریپو (commit پایه `18e445d`؛ ۹۵ تست سبز؛ head مایگریشن `0005_data_source_provenance`)
- تصمیم‌های کلیدی مستندشده: مدل `KpiDefinition` (تعریف ساختاریافته بدون SQL — ۵ تجمیع، فیلتر allow-list، گروه‌بندی/سری روزانه/هفتگی/ماهانه)، موتور محاسباتی pure روی رکوردهای در-حافظه (بدون SQL، قابل تست unit بدون DB)، **compute-on-read بدون cache/snapshot** (ساده‌ترین گزینه درست با حجم fact محدود)، ۷ اندپوینت `/kpis` با ماتریس نقش (مدیریت manager+، مصرف analyst+، viewer هیچ)، RLS ENABLE+FORCE+Fail-Closed مطابق قرارداد، migration خطی `0006_kpi_definitions` از `0005`
- توالی پیاده‌سازی در ۴ گام کوچک مستقل-آزمون‌پذیر با معیار پذیرش و نقطه توقف مشخص (§10)؛ non-goals صریح: AI/LLM، پیش‌بینی، ناهنجاری، arbitrary SQL، ETL، cache، موتورهای دیتابیس دیگر، بازطراحی fact_rows، داشبورد
- وضعیت برنامه‌ریزی: **COMPLETE** — سند طرح نهایی و push شد (commit `20e72ae`)

**فاز ۲.۳ — گام ۱: foundation تعریف KPI — تکمیل‌شده:**
- مدل `KpiDefinition` (`app/models/kpi_definition.py`): دقیقاً یک `data_source_id` (FK CASCADE)، `aggregation` ∈ sum/avg/min/max/count، `filters` JSONB ساختاریافته (هرگز SQL — allow-list فیلد/op، سقف ۱۰)، `group_by`/`granularity` با قاعده «granularity اجباری iff group_by=date»، پنجره تاریخ inclusive، `enabled`، یونیک name per-org (`uq_kpi_org_name`)؛ بدون ستون SQL/cache/snapshot/comparison/derived (§9 طرح)
- اعتبارسنجی تعریف: `validate_kpi_definition` + `KpiDefinitionError` در همان ماژول — قواعد تعریف فقط (هیچ محاسبه‌ای پیاده نشده)
- migration `0006_kpi_definitions` روی `0005` (تک‌head، بدون create_all، parity کامل مدل/migration — شامل JSONB و دو ایندکس)
- RLS runtime در `app/main.py`: ENABLE + FORCE + Fail-Closed org-scoped برای `kpi_definitions` (همان الگوی جداول دیگر)؛ role اپ همچنان NOSUPERUSER/NOBYPASSRLS
- تست‌ها: ۱۳ unit (`test_kpi_definition_unit.py` روی SQLite: defaults، قواعد تعریف، سقف فیلترها، یونیک per-org، cascade منبع، round-trip فیلتر ساختاریافته، نبود فیلدهای ممنوع) + ۷ integration روی PostgreSQL واقعی (`test_kpi_rls_postgres.py`: ENABLE/FORCE/qual fail-closed/WITH CHECK، درج بدون context رد می‌شود، cross-org بلاک، cascade واقعی، schema ستون‌ها/ایندکس‌ها)
- گیت: **۱۱۵ passed، 0 failed، 0 skipped** (۹۵ قبلی + ۲۰ جدید)؛ زنجیره روی دیتابیس خالی ۶/۶ تا `0006_kpi_definitions`، تک‌head، `alembic check` پاک قبل و بعد از تست‌ها
- گام‌های بعدی فاز ۲.۳ (شروع نشده): گام ۲ موتور pure KPI، گام ۳ API، گام ۴ سری + گیت نهایی

**فاز ۲.۳ — گام ۲: موتور pure KPI — تکمیل‌شده:**
- ماژول `app/services/kpi.py` — بدون هیچ SQL و دسترسی DB (تأیید با اسکن): ورودی مجموعه `FactRecord` در-حافظه + تعریف validated؛ حساب کاملاً Decimal (float عمداً پذیرفته نمی‌شود — حتی در adapter)، خروجی quantize به ۴ رقم با ROUND_HALF_UP مطابق Numeric(18,4)، نمای wire به‌صورت decimal-string
- Adapter `FactRecord.from_fact_row` با نام فیلدهای واقعی fact_rows (measure_value/dimension_date/dimension_category/dimension_label) — بدون import مدل/DB؛ ورودی غیرقابل‌نگاشت (float/bool/رشته نامعتبر/تاریخ نامعتبر/شیء غیر-FactRecord) → `KpiComputationError` typed، هرگز نتیجه اشتباه خاموش
- قرارداد §3.3 دقیق: empty → sum/count=0، avg/min/max=None، گروه‌ها=[]؛ measure=None دفاعی از همه تجمیع‌ها حذف؛ dimension NULL هرگز match فیلتر نمی‌شود و bucket نمی‌سازد
- پنجره inclusive + فیلترهای ساختاریافته گام ۱ (AND؛ date-filter همان مکانیزم پنجره)؛ گروه‌بندی day/week/month با کلید ISO (هفته ISO دوشنبه‌شروع با iso-year درست در مرز سال)، category/label مرتب صعودی؛ فقط bucketهای مشاهده‌شده — بدون zero-fill؛ ترتیب ورودی DB اثری بر خروجی ندارد
- اعتبارسنجی دفاعی تعریف قبل از محاسبه (§3.6) — همان قواعد گام ۱؛ engine database-agnostic و tenant-blind است (ایزولاسیون مسئول لایه بالادستی)
- تست‌ها: ۳۵ unit بدون DB (`tests/test_kpi_engine_unit.py`) — پوشش هر ۲۰ محور درخواستی شامل مرز سال ISO (2027-01-01 → 2026-W53)، دقت 0.1×10=1.0000 بدون drift شناور، ROUND_HALF_UP، بی‌اثر بودن ترتیب ورودی
- گیت: **۱۵۰ passed، 0 failed، 0 skipped** (۱۱۵ قبلی + ۳۵ جدید)؛ بدون migration جدید (گام ۲ هیچ تغییر schema ندارد)؛ Step 1 دست‌نخورده
- گام‌های بعدی فاز ۲.۳ (شروع نشده): گام ۴ سری + گیت نهایی

**فاز ۲.۳ — گام ۳: API تعریف KPI — تکمیل‌شده:**
- راستر `app/routers/kpis.py` — دقیقاً ۶ اندپوینت طرح §4 (بدون /series — گام ۴):
    `POST /kpis` و `PATCH /kpis/{id}` و `DELETE /kpis/{id}` → **manager+**؛
    `GET /kpis` و `GET /kpis/{id}` و `POST /kpis/{id}/compute` → **analyst+**؛ viewer هیچ دسترسی‌ای
- اعتبارسنجی فقط از قواعد گام ۱ (`validate_kpi_definition` — منبع واحد قواعد؛ بدون قاعده تکراری) → نقض → 422؛ نام تکراری در org → 409؛ سازمان نامشخص/غیرعضو → همان قراردادهای `require_role` موجود
- organization_id هرگز writable نیست — از membership تأییدشده می‌آید؛ همه کوئری‌ها org-scoped (RLS + فیلتر صریح — لایه ۳ دفاع کنار RLS)
- DataSource فقط از همان org و فقط usable (status == "mapped") قابل ارجاع/محاسبه — cross-org منبع → 404، منبع map نشده → 400
- compute: هیچ logic تجمیع/فیلتر/گروه‌بندی در راستر نیست — فقط fetch تننت-scoped fact_rows (فیلتر organization_id + data_source_id) → `FactRecord.from_fact_row` → `compute_kpi` موتور pure گام ۲ (تست spy عبور اجباری از موتور را ثابت می‌کند)
- خروجی compute قرارداد گام ۲: value به‌صورت decimal-string ۴رقم (هرگز float)؛ empty → sum/count="0.0000"، avg/min/max=null با HTTP 200؛ KPI غیرفعال → 400؛ cross-org id → 404 (بدون افشای وجود) روی همه اندپوینت‌ها
- اسکیماهای `app/schemas/kpi.py` — `password`/credential فیلدی وجود ندارد؛ هیچ راز/DSN در پاسخ/خطا نیست
- بدون migration جدید (Step 3 هیچ تغییر schema لازم نداشت)؛ بدون تغییر 0006؛ `alembic current` = `0006_kpi_definitions` تک‌head، `alembic check` پاک
- تست‌ها: ۱۸ integration روی PostgreSQL واقعی (`tests/test_kpis_api.py`) — ماتریس نقش‌ها (viewer 403 روی همه، analyst ساخت 403)، org-scoped list/get، cross-org 404 روی get/compute/delete، دزدیدن منبع cross-org رد، نام تکراری 409، همه قواعد اعتبارسنجی 422 (aggregation/group_by/granularity iff/filters/>10/پنجره معکوس)، revalidate کامل PATCH (granularity روی category → 422)، منبع pending → 400، delete، compute sum/avg/min/max/count با مقدار دقیق decimal-string ("60.7500")، فیلتر alpha=40.7500، پنجره inclusive (مرزها داخل)، empty semantics با HTTP 200، KPI غیرفعال 400، عبور اجباری از موتور pure (spy)، بدون افشای credential
- گیت: **۱۶۸ passed، 0 failed، 0 skipped** (۱۵۰ قبلی + ۱۸ جدید) در یک session؛ ماژول جدید rerun-clean
- گام بعدی فاز ۲.۳ (شروع نشده): گام ۴ سری + گیت نهایی

**فاز ۲.۳ — گام ۴: سری KPI + گیت نهایی — تکمیل‌شده (فاز ۲.۳ کامل شد):**
- `GET /kpis/{id}/series` (گام ۴) — seventh endpoint طرح §4؛ **analyst+**؛ viewer → 403؛ cross-org id → 404 (بدون افشای وجود)؛ KPI غیرفعال → 400؛ منبع map نشده → 400 (همان سمانتیک compute)
- سری هیچ logic گروه‌بندی/تجمیعی در راستر ندارد — fetch تننت-scoped fact_rows (فیلتر organization_id + data_source_id کنار RLS) → `FactRecord.from_fact_row` → `compute_kpi_series` موتور pure گام ۲ (تست spy عبور اجباری از موتور را ثابت می‌کند)
- شکل پاسخ دقیق §3.5: `{kpi_id, group_by, buckets:[{key, value, rows}]}` — key ISO-compatible (day/week-ISO/month)، فقط bucketهای مشاهده‌شده، مرتب‌سازی صعودی، **بدون zero-fill** (صفر-پُرکردن تقویم = نگرانی کلاینت، §9)
- مقدارها decimal-string ۴رقم (قرارداد گام ۲ — هرگز float)؛ سری خالی → HTTP 200 با `buckets: []`
- بدون مقایسه/رشد/پیش‌بینی — صرفاً سری تعریف‌شده
- اسکیما: `KpiBucketOut` + `KpiSeriesOut` در `app/schemas/kpi.py` — value به‌صورت string، بدون float
- بدون migration جدید (هیچ تغییر schema لازم نبود)؛ بدون تغییر 0006؛ `alembic current` = `0006_kpi_definitions` تک‌head، `alembic check` پاک
- تست‌ها: ۱۴ integration روی PostgreSQL واقعی (`tests/test_kpi_series_api.py`) — ماتریس نقش‌ها (analyst/owner ✓، viewer 403، unauth 401/403)، cross-org 404، disabled 400، منبع pending → 400، گروه‌بندی day (۵ bucket صعودی)، week (ISO صحیح در مرز سال: 2026-W02..W06)، month (۲ bucket)، پنجره inclusive دوطرفه (هر دو مرز داخل)، فیلتر category اعمال‌شده، سری خالی → 200/[]، دقت Decimal (avg alpha → "14.0833")، ترتیب deterministic (دو فراخوانی یکسان)، عبور اجباری از موتور pure (spy)، هم‌زیستی compute (جمع bucketها == مقدار اسکالر)
- گیت نهایی فاز ۲.۳: **۱۸۲ passed، 0 failed، 0 skipped** (۱۶۸ قبلی + ۱۴ جدید) در یک session؛ ماژول جدید rerun-clean
- **همه دروازه‌های پذیرش §10 فاز ۲.۳ سبز شدند** — موتور pure + سری + API با RLS روی PostgreSQL واقعی؛ بدون cache/snapshot/AI/داشبورد؛ نقش اپ NOSUPERUSER+NOBYPASSRLS؛ زنجیره migration خطی تک‌head

**فاز ۲.۴ — کاتالوگ زمینه — کامل شد:**
- سند مرجع: **`docs/PHASE4_CONTEXT_CATALOG_PLAN.md`** — طرح پیاده‌سازی بر پایه شواهد واقعی ریپو (commit پایه `a7a5ba4`؛ ۱۸۲ تست سبز؛ head مایگریشن `0006_kpi_definitions`)
- هدف: لایه زمینه ساختاریافته/deterministic/tenant-scoped برای دستیار AI آینده — نه خود AI؛ پاسخ به «چه داده‌ای داریم، هر منبع چه معنایی دارد، چه KPIهایی تعریف شده‌اند، چه گروه‌بندی‌هایی ممکن است»
- تصمیم‌های کلیدی مستندشده: **بدون جدول جدید و بدون migration** (همه متادیتای لازم از قبل در data_sources/data_source_columns/kpi_definitions/fact_rows/database_connections هست و تحت RLS است)؛ **محاسبه-on-read** (همان استدلال فاز ۲.۳ §3.7 — حجم محدود، invalidation غیرضروری)؛ Context Builder ی pure در `app/services/context_catalog.py` با قرارداد خروجی نسخه‌دار (`schema_version`) و سقف‌های صریح (`MAX_SOURCES=200`، `MAX_KPIS=200`، `MAX_COLUMNS_PER_SOURCE=200`، `MAX_DIMENSION_VALUES=50`) — بدون هیچ SQL خام؛ پوشش fact لایه به‌صورت تجمیع‌های bounded (تعداد ردیف، بازه تاریخ، واژه‌نامه ابعاد، last_mapped_at = max(created_at))؛ یک اندپوینت فقط-خواندنی `GET /context/catalog` با analyst+ (viewer ممنوع)؛ هیچ مقدار KPI محاسبه‌شده در کاتالوگ نیست — اعداد از اندپوینت‌های موجود compute/series می‌آیند؛ secrets اتصالات هرگز وارد payload نمی‌شوند (تست non-disclosure اجباری)
- پیاده‌سازی در ۳ گام کوچک مستقل-آزمون‌پذیر (§12): builder+schemas+unit → API+integration → گیت نهایی؛ non-goals صریح: AI/LLM/embeddings/RAG، anomaly/forecasting/recommendations، dashboard، جدول/migration جدید، cache، arbitrary SQL، موتور دیتابیس دیگر، بازطراحی fact_rows
- وضعیت برنامه‌ریزی: **PLANNING / NOT STARTED** — سند طرح آماده است؛ منتظر دستور پیاده‌سازی گام ۱

**فاز ۲.۴ — گام ۱: builder خالص + اسکیماها — تکمیل‌شده:**
- سرویس `app/services/context_catalog.py` — builder خواندن-محور deterministic و tenant-scoped: بدون جدول جدید و بدون migration (همه متادیتا از data_sources/data_source_columns/kpi_definitions/fact_rows زیر RLS خوانده می‌شود)؛ تنها ORM و تجمیع‌های func.min/max/count — **هیچ رشته SQL** در ماژول نیست؛ organization_id پارامتر keyword-only اجباری (fail-closed روی None) و هرگز از کلاینت نمی‌آید
- پوشش fact لایه به‌صورت تجمیع‌های bounded per-source (§8): count، min/max(dimension_date)، last_mapped_at = max(created_at) (سیگنال رایگان freshness — map همیشه delete+re-insert است)، واژه‌نامه distinct ابعاد با LIMIT MAX_DIMENSION_VALUES+1 به‌عنوان sentinel + COUNT(DISTINCT) جداگانه تا true total حتی با truncate دقیق بماند؛ هیچ کوئری‌ای fact_rows را بدون هر دو فیلتر organization_id + data_source_id اسکن نمی‌کند
- سقف‌های صریح (§8): MAX_SOURCES=200 / MAX_KPIS=200 / MAX_COLUMNS_PER_SOURCE=200 / MAX_DIMENSION_VALUES=50 — summary.data_source_count و summary.kpi_count همیشه true total (نه طول لیست‌های truncate)
- determinism (§4 rule 1): منابع با uploaded_at desc + tiebreaker name asc (تقویت determinism — uploaded_at به‌تنهایی یکتا نیست؛ همین نکته در طرح اصلاح مستندسازی شد)، KPIها با created_at desc + name asc (دقیقاً ترتیب GET /kpis)، ستون‌ها با position، ابعاد صعودی مرتب‌شده در Python مستقل از ترتیب DB
- اسکیماها `app/schemas/context_catalog.py` — پاکت نسخه‌دار (schema_version=1)، بدون هیچ float، بدون هیچ فیلد credential؛ از اتصال خارجی فقط database_connection_id (پاریته DataSourceOut)؛ **هیچ مقدار KPI محاسبه‌شده** در کاتالوگ نیست — اعداد از compute/series موجود می‌آیند
- تست‌ها: ۱۴ unit بدون PostgreSQL (`tests/test_context_catalog_unit.py` روی SQLite در-حافظه، الگوی test_storage_unit.py) — org خالی، determinism ترتیب‌ها، پوشش صفر برای منبع unmapped، تجمیع‌ها + freshness دقیق، date_span سراسری، سقف ابعاد با true total، سقف KPI/منبع با truncate newest-first، بدون float در کل payload و بدون هیچ راز، تعریف KPI بدون فیلد محاسبه‌شده (value/buckets)
- گیت: **۱۹۶ passed، 0 failed، 0 skipped** در یک session روی PostgreSQL واقعی (۱۸۲ baseline + ۱۴ جدید)؛ `alembic current` = `0006_kpi_definitions` تک‌head؛ `alembic check` پاک (گام ۱ صفر migration دارد — هر تغییر schema خطای گیت است)؛ role اپ همچنان NOSUPERUSER/NOBYPASSRLS؛ RLS دست‌نخورده (۷ جدول)
- گام بعدی فاز ۲.۴ (شروع نشده): گام ۲ اندپوینت GET /context/catalog + تست‌های integration (ماتریس نقش، ایزولاسیون cross-org، determinism HTTP، non-disclosure با اتصال واقعی)؛ سپس گام ۳ گیت نهایی

**فاز ۲.۴ — گام ۲: اندپوینت API + تست‌های integration — تکمیل‌شده:**
- راستر `app/routers/context_catalog.py` با **تنها اندپوینت طرح §9**: `GET /context/catalog` → **analyst+** (["owner","admin","manager","analyst"])؛ viewer → 403؛ unauthenticated → 401؛ بدون سازمان (نه هدر و نه claim JWT) → 400؛ ثبت در app/main.py بعد از kpis (یک خط import + یک include_router)
- organization_id هرگز از کلاینت پذیرفته نمی‌شود — از membership تأییدشده (require_role → require_membership → set_rls_context)؛ کوئری‌های builder تحت RLS context درخواست اجرا می‌شوند؛ **هیچ path/query parameter ای وجود ندارد** — سمانتیک 404 ندارد و filtering کار لایه AI آینده است؛ org خالی → 200 با کاتالوگ معتبر خالی (قرارداد empty فاز ۲.۳ §3.3)؛ هیچ منطق catalogی در راستر نیست — فقط فراخوانی builder خالص گام ۱
- تست‌ها: ۹ integration روی PostgreSQL واقعی (`tests/test_context_catalog_api.py`، الگوی test_kpis_api.py — seed در fixture، override داخل fixture، cleanup فقط ردیف‌های خود ماژول با پیشوند ctx-api-/ctx-*-catalog): ماتریس نقش‌ها، org خالی، **ایزولاسیون tenant دوطرفه** (دو org با منابع/KPI هم‌نام — صفر نشت identifier در هر دو جهت حتی با هم‌نامی)، determinism HTTP (دو فراخوانی یکسان بجز generated_at)، پوشش end-to-end (mapped_role ستون‌ها، coverage با مقادیر دستی-محاسبه‌شده روی seed واقعی، provenance اتصال روی منبع mapped و import شده)، lifecycle (ساخت/حذف KPI از API → کاتالوگ به‌روز؛ حذف منبع → KPI/fact cascade و کاتالوگ به‌روز؛ حذف اتصال → SET NULL در کاتالوگ)، **non-disclosure با اتصال واقعی** (رمز encrypt شده در DB؛ host/username/dbname/password/ciphertext هرگز در payload نیست)؛ یک یافته تست: توکن دارای claim org_id به‌عنوان fallback سازمان معتبر است (رفتار established get_current_organization_id) — تست «بدون سازمان» با توکن بدون claim اصلاح شد
- گیت: **۲۰۵ passed، 0 failed، 0 skipped** در یک session روی PostgreSQL واقعی (۱۹۶ قبلی + ۹ جدید)؛ بدون migration (گام ۲ هیچ تغییر schema ندارد)؛ `alembic current` = `0006_kpi_definitions` تک‌head؛ `alembic check` پاک؛ role اپ NOSUPERUSER/NOBYPASSRLS؛ RLS دست‌نخورده
- گام بعدی فاز ۲.۴ (شروع نشده): گام ۳ گیت نهایی (checklist پذیرش §12 + علامت‌گذاری COMPLETE)

**فاز ۲.۴ — گام ۳: گیت نهایی — تکمیل‌شده (فاز ۲.۴ کامل شد):**
- checklist پذیرش §12 همگی سبز شد:
  - **صفر migration در کل فاز ۲.۴** — `git diff` روی `alembic/` از کامیت برنامه‌ریزی خالی است؛ زنجیره بدون تغییر `0001 → 0006`؛ `alembic current` = `0006_kpi_definitions`؛ دقیقاً یک head؛ `alembic check` = «No new upgrade operations detected» **قبل و بعد از** اجرای نهایی تست‌ها
  - **RLS زنده تأیید شد** (کوئری pg_class روی دیتابیس تست): هر ۷ جدول (memberships/data_sources/fact_rows/data_source_files/data_source_columns/database_connections/kpi_definitions) هم `relrowsecurity=t` (ENABLE) و هم `relforcerowsecurity=t` (FORCE)؛ صفر پالیسی حاوی `IS NULL` (fail-closed)؛ نقش اپ همچنان `rolsuper=f` + `rolbypassrls=f` (NOSUPERUSER/NOBYPASSRLS)
  - **قرارداد پاکت §4 با تست اثبات شد**: schema_version نسخه‌دار، بدون float در کل payload، بدون هیچ فیلد credential (فقط provenance id)، بدون هیچ مقدار KPI محاسبه‌شده، determinism کامل بجز generated_at (۱۴ unit + ۹ integration)
  - **non-disclosure با ردیف اتصال واقعی اثبات شد**: رمز encrypt شده در DB؛ host/username/database_name/password هرگز در پاسخ HTTP نیستند
  - **اسکن ایستا**: هیچ رشته SQL در `app/services/context_catalog.py` (فقط ORM)؛ هیچ cache/snapshot/AI/LLM/forecast در کد یا `requirements.txt`؛ fact_rows و connector سطح دست‌نخورده
- گیت نهایی: **۲۰۵ passed، 0 failed، 0 skipped** در یک session روی PostgreSQL واقعی — دو بار اجرا شد (پیش و پس از checklist)؛ ۱۸۲ baseline بدون رگرسیون (+ ۲۳ تست فاز ۲.۴: ۱۴ unit + ۹ API)
- **وضعیت نهایی: فاز ۲.۴ COMPLETE** — کاتالوگ زمینه (builder خالص + `GET /context/catalog`) آماده مصرف لایه AI آینده است؛ گام بعدی نقشه راه (فاز ۲.۵+ — لایه تصمیم AI) شروع نشده است

**فاز ۲.۵ — لایه تصمیم AI — برنامه‌ریزی شد (شروع پیاده‌سازی نشده):**
- سند مرجع: **`docs/PHASE5_AI_PLAN.md`** — طرح پیاده‌سازی بر پایه شواهد واقعی ریپو (commit پایه `c319241`؛ ۲۰۵ تست سبز؛ head مایگریشن `0006_kpi_definitions`)
- هدف: پاسخ به «چه خبر است؟» و «کدام KPI تغییر کرده؟» — تشخیص تغییر deterministic (دو پنجره برابر مجاور، تغییر مطلق/نسبی، movers ابعاد از سری موجود) + بریفینگ سازمانی؛ LLM فقط **روایت‌گر** است و هرگز عدد محاسبه نمی‌کند؛ قاعده حاکم: «اعداد از کد، کلمات از مدل»
- تصمیم‌های کلیدی مستندشده: موتور insight خالص بدون DB/SQL در `app/services/insights.py` (Decimal، بدون تقسیم بر صفر، بدون zero-fill، `MAX_MOVERS=5`، آستانه ±۲۵٪ به‌عنوان change detection — نه علم ناهنجاری)؛ انتزاع `LlmProvider` با قرارداد None-when-unconfigured (بدون کلید در dev → پاسخ deterministic با `narrative: null` — LLM هرگز روی مسیر عددی اثر ندارد)؛ **SambaNova** به‌عنوان ارائه‌دهنده پیاده‌سازی‌شده (OpenAI-compatible REST با httpx موجود — بدون SDK جدید؛ کلید `SAMBANOVA_API_KEY` با الگوی production fail-fast موجود)؛ قرارداد prompt نسخه‌دار و bounded (فقط insight packet JSON — هرگز credential/SQL/row خام/داده org دیگر؛ فایروال injection: داده‌های کاربر داخل JSON منتقل می‌شوند و خروجی schema-validated و طول‌سقف‌دار است)؛ **صفر جدول جدید و صفر migration** (compute-on-read — همان استدلال فازهای ۲.۳/۲.۴)؛ دو اندپوینت فقط-خواندنی analyst+ (`GET /kpis/{id}/insight` + `GET /assistant/briefing`)؛ تست‌ها هرگز LLM واقعی صدا نمی‌زنند (MockTransport + provider پایتونی stub)
- پیاده‌سازی در ۴ گام کوچک مستقل-آزمون‌پذیر (§10): insight engine → provider → API → گیت نهایی؛ non-goals صریح: action خودکار/زمان‌بندی/alert، chat UI/حافظه مکالمه، NL-to-SQL (SQL دلخواه همچنان ممنوع کل ریپو)، علم ناهنجاری/پیش‌بینی/توصیه، embeddings/vector/RAG، fine-tuning/agent framework، rotation چند-provider، ماندگاری insight، داشبورد، جدول/migration جدید
- وضعیت برنامه‌ریزی: **COMPLETE** — سند طرح نهایی و push شد (commit `00e3b26`)

**فاز ۲.۵ — گام ۱: موتور insight خالص — تکمیل‌شده:**
- ماژول `app/services/insights.py` — بدون هیچ SQL/DB/LLM/شبکه (تأیید با اسکن ایستا و تست): ورودی فقط «مصنوعات از-پیش-محاسبه‌شده» موتور pure فاز ۲.۳ است (`KpiResult` دو پنجره برابر مجاور + جفت اختیاری `GroupedResult`)؛ خروجی `ChangePacket`/`Mover` (§2.1 طرح) با همه مقادیر عددی به‌صورت decimal-string ۴رقم
- قرارداد §2.1 دقیق: empty/missing → `direction="unknown"` و `flagged=False` و مقادیر None (موفقیت است نه خطا، بدون عدد ساختگی)؛ تقسیم فقط برای `change_pct` با مبنای قدرمطلق (علامت همیشه هم‌علامت change؛ previous=0 → `change_pct=None` — تقسیم بر صفر وجود ندارد)؛ flat = مقایسه exact روی Decimal؛ quantize دفاعی ورودی‌ها به ۴ رقم با ROUND_HALF_UP (قرارداد Numeric(18,4) حتی برای ورودی خارج از موتور)
- آستانه §2.2: `ANOMALY_THRESHOLD = 0.25` — پرچم فقط وقتی `|change_pct| >= 0.25` و هر دو پنجره مقدار دارند (تشخیص تغییر؛ ناهنجاری آماری non-goal است)
- movers: فقط bucketهای مشاهده‌شده در هر دو پنجره با مقدار non-None در هر دو سمت (بدون zero-fill)؛ contribution = مقدار جاری − همان bucket قبلی؛ مرتب‌سازی نزولی |contribution| با تای‌بریک key صعودی؛ سقف `MAX_MOVERS=5`؛ سری‌های دو پنجره باید group_by یکسان داشته باشند و فقط به‌صورت جفت پذیرفته می‌شوند
- ورودی نامعتبر → `InsightInputError` typed (هرگز نتیجه اشتباه خاموش)؛ ساختارها frozen؛ deterministic مستقل از ترتیب bucketهای ورودی
- تست‌ها: ۳۴ unit بدون DB (`tests/test_insights_unit.py`، الگوی test_kpi_engine_unit.py) — up/down/flat/unknown، مرز دقیق آستانه (25% == پرچم، 24.9999% گردشده == پرچم — قرارداد گرد شدن ۴رقم documented)، previous=0، مبنا منفی، دقت Decimal، مرتب‌سازی/تای/سقف movers، حذف bucketهای یک‌طرفه و None، determinism، خطاهای typed، بدون float در کل خروجی، اسکن ایستای نبودِ SQL/DB/LLM، انتها-به-انتها با موتور واقعی فاز ۲.۳ (شامل: کلیدهای تاریخ دو پنجره مجاور هرگز مشترک نیستند → movers=[] بدون zero-fill)
- گیت: **۲۳۹ passed، 0 failed، 0 skipped** در یک session روی PostgreSQL واقعی (۲۰۵ baseline + ۳۴ جدید — بدون رگرسیون)؛ بدون migration جدید (گام ۱ هیچ تغییر schema ندارد)؛ `alembic current` = `0006_kpi_definitions` تک‌head؛ `alembic check` پاک؛ بدون هیچ کد AI/provider/router — `app/services/ai/` و راسترها وجود ندارند (گام‌های بعدی)
- گام‌های بعدی فاز ۲.۵ (شروع نشده): گام ۲ provider abstraction + SambaNova، گام ۳ API، گام ۴ گیت نهایی

**فاز ۲.۵ — گام ۲: انتزاع provider + SambaNova — تکمیل‌شده:**
- بسته `app/services/ai/` (§2.3/§7 طرح): `base.py` (ABC `LlmProvider` + خطاهای typed sanitized `AiProviderError`/`AiTimeoutError`/`AiBadResponseError` با code های `provider_error`/`provider_unavailable`/`bad_response` + `NarrationResult` + `validate_narration` با سقف‌های §2.4: summary ≤ 2000، highlights ≤ 10×500 — مقادیر غیر-رشته‌ای رد می‌شوند، هرگز coerce خاموش نیست) · `prompts.py` (سیستم prompt ثابت نسخه‌دار `SYSTEM_PROMPT_VERSION=1`؛ پیام user دقیقاً `json.dumps(packet)` — firewall injection §2.4: داده‌های کاربر داخل JSON منتقل می‌شوند نه داخل دستورها) · `sambanova.py` (OpenAI-compatible chat-completions با httpx موجود — بدون SDK جدید؛ `response_format: {"type": "json_object"}`؛ `temperature=0`؛ timeout سخت ۲۰s؛ کلید فقط در هدر Authorization، هرگز در لاگ/خطا/پاسخ؛ خطای non-200 فقط status code — بدون بدنه خام که می‌تواند کلید/داده tenant را echo کند) · `factory.py` (`get_llm_provider()` با قرارداد **None-when-unconfigured** — dev بدون کلید → None و پاسخ deterministic با `narrative: null`؛ ENV=production بدون کلید → RuntimeError الگوی JWT_SECRET/ENCRYPTION_KEY؛ AI_PROVIDER ناشناخته → AiProviderError؛ rotation چند-provider non-goal)
- settings (`app/core/config.py`): `AI_PROVIDER`/`AI_MODEL`/`AI_BASE_URL`/`AI_API_KEY` اضافه شد — کلید با ترتیب AI_API_KEY → SAMBANOVA_API_KEY (env سپس .env) resolve می‌شود؛ نبودِ کلید در production خطای صریح هنگام load settings؛ در dev sentinel «پیکربندی‌نشده» است و factory آن را None تفسیر می‌کند (LLM هرگز روی مسیر عددی اثر ندارد)
- تست‌ها: ۱۷ unit بدون شبکه واقعی (`tests/test_ai_provider_unit.py` — SambaNovaProvider فقط با `httpx.MockTransport`): شکل درخواست (URL/هدر/`temperature=0`/`response_format`/سیستم prompt نسخه‌دار/user دقیقاً packet)، موفقیت → NarrationResult، non-200 sanitized (تست صریح: کلید در بدنه خطا نیست)، JSON بد/بدون content/content غیر-JSON/۶ نوع نقض schema/timeout/connect-error typed، سقف‌های طول، factory (بدون کلید → None، با کلید env/alias → provider، provider ناشناخته → error، production بدون کلید → RuntimeError، production با کلید → provider)، قرارداد ABC
- گیت: **۲۵۶ passed، 0 failed، 0 skipped** در یک session روی PostgreSQL واقعی (۲۳۹ قبلی + ۱۷ جدید — بدون رگرسیون)؛ بدون migration جدید (گام ۲ هیچ تغییر schema ندارد)؛ `alembic current` = `0006_kpi_definitions` تک‌head؛ `alembic check` پاک؛ هیچ endpoint ای وجود ندارد (گام ۳) و هیچ فراخوانی LLM واقعی در تست‌ها انجام نمی‌شود
- گام بعدی فاز ۲.۵ (شروع نشده): گام ۳ API (`GET /kpis/{id}/insight` + `GET /assistant/briefing`)، گام ۴ گیت نهایی

## بدهی فنی شناخته‌شده

- **مایگریشن دیتابیس:** تا فاز ۱ schema فقط با `create_all` ساخته می‌شد — از فاز ۲.۰ با Alembic مدیریت می‌شود (baseline: `0001_baseline`). تغییرات schema آینده فقط با migration جدید.
- **CI وجود ندارد** (پوشه `.github/` نیست)؛ تست‌ها فقط به‌صورت محلی اجرا می‌شوند.
- **پیکربندی pytest وجود ندارد** (`conftest.py`/`pytest.ini`/`pyproject.toml` نیست)؛ path و env توسط خود فایل‌های تست ست می‌شود.
- تست‌های integration بدون PostgreSQL در دسترس skip می‌شوند؛ اجرای کامل نیاز به `docker compose -f docker-compose.test.yml up -d` دارد.
- مقدارهای fallback رمزهای dev در `config.py` فقط برای dev هستند (production با RuntimeError متوقف می‌شود) — حذف کامل آن‌ها بعد از استقرار مدیریت secretها.
- این سند جدید است: `docs/PHASE_STATUS.md` پیش‌تر در تاریخچه git وجود نداشت.
