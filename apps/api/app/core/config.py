import os

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # DEV_DATABASE_URL اولویت دارد (Neon dev جدا از test)، سپس DATABASE_URL
    DATABASE_URL: str = os.getenv("DEV_DATABASE_URL") or os.getenv("DATABASE_URL") or "postgresql+psycopg://tasmim:tasmim_secret@localhost:5432/tasmim_yar"
    JWT_SECRET: str = "change-me-super-secret-jwt-key"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440
    CORS_ORIGINS: str = "http://localhost:3000"

    class Config:
        env_file = ".env"


settings = Settings()
