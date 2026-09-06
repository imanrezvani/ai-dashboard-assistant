"""اجرای Alembic migration در startup اپ — فاز ۲.۰

جایگزین `Base.metadata.create_all` در app/main.py.

منطق:
  - فقط روی PostgreSQL اجرا می‌شود؛ در dialectهای دیگر (مثل SQLite در تست‌ها)
    no-op است تا رفتار فعلی تست‌ها بدون تغییر بماند.
  - `pg_advisory_xact_lock` جلوی migrate همزمان توسط چند instance را می‌گیرد
    (قفل تراکنشی — با پایان تراکنش آزاد می‌شود).
  - دیتابیس تازه (بدون هیچ جدولی)          → upgrade head (اعمال baseline)
  - دیتابیس موجود فاز ۱ (جداول هست، alembic_version نیست) → stamp head
  - دیتابیس stamp شده                       → upgrade head

خطای migration اپ را بالا نمی‌آید بی‌صدا رها کند: استثنا propagate می‌شود —
schema ناقص بهتر است fail-fast باشد تا drift پنهان.
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

API_DIR = Path(__file__).resolve().parents[2]
ADVISORY_LOCK_KEY = "tasmim_yar_alembic_migrations"


def _alembic_config() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    # script_location مطلق تا از هر cwd (docker/uvicorn/pytest) اجرا شود
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    return cfg


def run_startup_migrations(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        logger.info("[migrations] skip (dialect=%s) — فقط PostgreSQL", engine.dialect.name)
        return

    cfg = _alembic_config()
    with engine.begin() as conn:
        # قفل مشورتی تراکنشی: فقط یک instance همزمان migrate می‌کند
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": ADVISORY_LOCK_KEY})
        cfg.attributes["connection"] = conn  # حالت in-process در alembic/env.py

        inspector = inspect(conn)
        has_version_table = inspector.has_table("alembic_version")
        has_phase1_schema = inspector.has_table("organizations")

        if not has_version_table:
            if has_phase1_schema:
                logger.info("[migrations] دیتابیس موجود بدون alembic_version → stamp baseline")
                command.stamp(cfg, "head")
            else:
                logger.info("[migrations] دیتابیس تازه → اعمال baseline")
                command.upgrade(cfg, "head")
        else:
            logger.info("[migrations] اجرای upgrade head")
            command.upgrade(cfg, "head")

    logger.info("[migrations] schema به‌روز است (head)")
