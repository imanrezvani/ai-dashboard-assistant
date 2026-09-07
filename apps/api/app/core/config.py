import os

from pydantic_settings import BaseSettings


def _get_secret(name: str, dev_fallback: str) -> str:
    val = os.getenv(name)
    if val:
        return val
    if os.getenv("ENV") == "production":
        raise RuntimeError(f"{name} must be set in production")
    return dev_fallback


# fallback لوکال فقط برای dev — در production هرگز نباید استفاده شود
_DEV_DATABASE_URL = "postgresql+psycopg://tasmim:tasmim_secret@localhost:5432/tasmim_yar"


def _dotenv_value(name: str) -> str | None:
    """خواندن مقدار از .env (همان semantics قبلی env_file='.env' pydantic) —
    برای اجرای بدون docker که کاربر DATABASE_URL را در .env می‌گذارد."""
    try:
        from dotenv import dotenv_values  # python-dotenv از وابستگی‌های pydantic-settings
        val = dotenv_values(".env").get(name)
        return val if val else None
    except Exception:
        return None


def _resolve_database_url() -> str:
    """DATABASE_URL را یک‌جا و با ترتیب مشخص resolve می‌کند.

    ترتیب اولویت:
      ۱. DEV_DATABASE_URL (فقط در non-production؛ دیتابیس dev جدا مثل Neon dev)
      ۲. DATABASE_URL
      ۳. fallback لوکال dev (فقط dev)

    نکته باگ: مقدار خالی ('') از env/dotenv نباید fallback را override کند —
    pydantic-settings به‌طور پیش‌فرض '' را معتبر می‌گیرد و باعث می‌شد
    create_engine('') با «Could not parse SQLAlchemy URL from string ''» بترکد.
    برای همین هم مقدارهای خالی اینجا نادیده گرفته می‌شوند و هم
    env_ignore_empty=True در Config ست شده است.
    """
    is_production = os.getenv("ENV") == "production"
    if not is_production:
        dev_url = os.getenv("DEV_DATABASE_URL")
        if dev_url:
            return dev_url
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    url = _dotenv_value("DATABASE_URL")
    if url:
        return url
    if is_production:
        raise RuntimeError("DATABASE_URL must be set in production")
    return _DEV_DATABASE_URL


class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str = _get_secret("JWT_SECRET", "dev-only-insecure-do-not-use-in-prod")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440
    CORS_ORIGINS: str = "http://localhost:3000"
    TASMIM_APP_DB_PASSWORD: str = _get_secret("TASMIM_APP_DB_PASSWORD", "tasmim_app_secret_dev_only")
    # فاز ۲.۲: رمزنگاری اعتبارنامه‌های اتصالات دیتابیس خارجی — در production الزامی
    # (برای تولید کلید: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    ENCRYPTION_KEY: str = _get_secret("ENCRYPTION_KEY", "dev-only-encryption-key-do-not-use-in-prod")

    class Config:
        env_file = ".env"
        # مقدار خالی ('') از env/.env نادیده گرفته می‌شود تا fallback معتبر بماند
        env_ignore_empty = True


# DATABASE_URL عمداً خارج از env-lookup خودکار pydantic ست می‌شود (init kwarg
# بالاترین اولویت را دارد) تا ترتیب اولویت بالا — به‌خصوص DEV_DATABASE_URL در dev —
# همیشه تضمین شود و مقدار خالی هرگز به create_engine نرسد.
settings = Settings(DATABASE_URL=_resolve_database_url())
