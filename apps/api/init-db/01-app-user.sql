-- ایزولاسیون RLS: ساخت role غیر-superuser برای اپلیکیشن
-- superuser حتی با FORCE ROW LEVEL SECURITY از RLS معاف است (BYPASSRLS)
-- بنابراین اپ نباید با superuser (tasmim) وصل شود.
-- این اسکریپت در اولین اجرای postgres (docker-entrypoint-initdb.d) اجرا می‌شود.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tasmim_app') THEN
    CREATE ROLE tasmim_app WITH LOGIN PASSWORD 'tasmim_app_secret' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END
$$;

-- دسترسی اولیه به دیتابیس (جداول بعداً توسط app/main.py ساخته می‌شوند و GRANT می‌گیرند)
GRANT CONNECT ON DATABASE tasmim_yar TO tasmim_app;
-- برای جداول آینده، GRANT در main.py:on_startup نیز انجام می‌شود
