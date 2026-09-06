"""Alembic environment for the FastAPI backend.

دو حالت:
  - online: اجرای migration روی دیتابیس واقعی (alembic upgrade head)
  - offline: تولید SQL برای DBA (--sql) بدون اتصال

URL همیشه از زنجیره خود اپ (app.core.config.settings: DEV_DATABASE_URL ←
DATABASE_URL ← dotenv ← fallback dev) می‌آید تا CLI و API هرگز URL متفاوت
نبینند. override یک‌باره هم ممکن است: `alembic -x db_url=...` (بدون ذخیره credential).

اگر از درون اپ صدا زده شود (app/core/migrations.py)، connection از
config.attributes["connection"] می‌آید تا migration روی همان connection و
زیر advisory lock اپ اجرا شود.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# اطمینان از import پکیج app هنگام اجرای CLI از هر مسیری
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from app.core.config import settings  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.models import *  # noqa: F401,F403,E402 - ثبت همه مدل‌ها روی Base.metadata

target_metadata = Base.metadata


def _database_url() -> str:
    # اولویت: override یک‌باره با -x db_url=... (بدون ذخیره credential در فایل)
    x_url = context.get_x_argument(as_dictionary=True).get("db_url")
    if x_url:
        return x_url
    return settings.DATABASE_URL


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    """تولید SQL بدون اتصال (--sql)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # حالت in-process (startup اپ): تراکنش و advisory lock بیرون مدیریت می‌شود
    shared_conn = config.attributes.get("connection")
    if shared_conn is not None:
        _configure(shared_conn)
        context.run_migrations()
        return

    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
