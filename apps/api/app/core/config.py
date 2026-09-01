import os

from pydantic_settings import BaseSettings


def _get_secret(name: str, dev_fallback: str) -> str:
    val = os.getenv(name)
    if val:
        return val
    if os.getenv("ENV") == "production":
        raise RuntimeError(f"{name} must be set in production")
    return dev_fallback


class Settings(BaseSettings):
    # DEV_DATABASE_URL اولویت دارد (Neon dev جدا از test)، سپس DATABASE_URL
    DATABASE_URL: str = os.getenv("DEV_DATABASE_URL") or os.getenv("DATABASE_URL") or "postgresql+psycopg://tasmim:tasmim_secret@localhost:5432/tasmim_yar"
    JWT_SECRET: str = _get_secret("JWT_SECRET", "dev-only-insecure-do-not-use-in-prod")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440
    CORS_ORIGINS: str = "http://localhost:3000"
    TASMIM_APP_DB_PASSWORD: str = _get_secret("TASMIM_APP_DB_PASSWORD", "tasmim_app_secret_dev_only")

    class Config:
        env_file = ".env"


settings = Settings()
