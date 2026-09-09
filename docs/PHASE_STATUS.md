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

فاز ۲: ۱) اتصال دیتابیس خارجی — **تکمیل** ۲) موتور KPI — برنامه‌ریزی شد (`docs/PHASE3_KPI_PLAN.md`) ۳) کاتالوگ زمینه (آماده‌سازی دستیار AI) — آینده

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
- گام‌های بعدی فاز ۲.۳ (شروع نشده): گام ۳ API، گام ۴ سری + گیت نهایی

## بدهی فنی شناخته‌شده

- **مایگریشن دیتابیس:** تا فاز ۱ schema فقط با `create_all` ساخته می‌شد — از فاز ۲.۰ با Alembic مدیریت می‌شود (baseline: `0001_baseline`). تغییرات schema آینده فقط با migration جدید.
- **CI وجود ندارد** (پوشه `.github/` نیست)؛ تست‌ها فقط به‌صورت محلی اجرا می‌شوند.
- **پیکربندی pytest وجود ندارد** (`conftest.py`/`pytest.ini`/`pyproject.toml` نیست)؛ path و env توسط خود فایل‌های تست ست می‌شود.
- تست‌های integration بدون PostgreSQL در دسترس skip می‌شوند؛ اجرای کامل نیاز به `docker compose -f docker-compose.test.yml up -d` دارد.
- مقدارهای fallback رمزهای dev در `config.py` فقط برای dev هستند (production با RuntimeError متوقف می‌شود) — حذف کامل آن‌ها بعد از استقرار مدیریت secretها.
- این سند جدید است: `docs/PHASE_STATUS.md` پیش‌تر در تاریخچه git وجود نداشت.
