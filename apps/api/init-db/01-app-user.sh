#!/bin/bash
set -e

# این اسکریپت در اولین اجرای postgres (docker-entrypoint-initdb.d) اجرا می‌شود
# رمز از متغیر محیطی TASMIM_APP_DB_PASSWORD خوانده می‌شود، نه hardcode
# اگر متغیر ست نشده باشد، از مقدار پیش‌فرض برای محیط dev لوکال استفاده می‌شود (فقط برای تست محلی)

APP_PASSWORD="${TASMIM_APP_DB_PASSWORD:-tasmim_app_secret_dev_only}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tasmim_app') THEN
    CREATE ROLE tasmim_app WITH LOGIN PASSWORD '${APP_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  ELSE
    -- اگر قبلاً وجود داشت، رمز را به‌روزرسانی کن (برای چرخش رمز)
    ALTER ROLE tasmim_app WITH LOGIN PASSWORD '${APP_PASSWORD}';
  END IF;
END
\$\$;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO tasmim_app;
EOSQL

echo "tasmim_app role ensured (NOBYPASSRLS) for DB ${POSTGRES_DB}"
