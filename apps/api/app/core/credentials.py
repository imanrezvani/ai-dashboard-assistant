"""رمزنگاری اعتبارنامه‌ها در حالت rest — فاز ۲.۲ گام ۱

قرارداد (طبق docs/PHASE2_PLAN.md §3):
  - فقط ciphertext در دیتابیس ذخیره می‌شود؛ plaintext هرگز persisted نمی‌شود
  - ENCRYPTION_KEY از settings می‌آید؛ در production نبود آن RuntimeError می‌دهد
    (همان قرارداد _get_secret مثل JWT_SECRET)
  - کلید Fernet از sha256(ENCRYPTION_KEY) مشتق می‌شود (۳۲ بایت، urlsafe-base64)
    تا هر secret قویِ عملیاتی قابل استفاده باشد؛ production کلید تصادفی قوی بگذارد
  - plaintext هرگز لاگ نمی‌شود (این ماژول هرگز چیزی لاگ نمی‌کند)
  - خرابی integrity (کلید اشتباه/دستکاری) → CredentialTampered — بدون افشای محتوا
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class CredentialError(Exception):
    """خطای عمومی لایه رمزنگاری اعتبارنامه."""


class CredentialTampered(CredentialError):
    """ciphertext با این کلید قابل تأیید نیست (کلید اشتباه یا داده دستکاری‌شده)."""


def _fernet() -> Fernet:
    """Fernet key مشتق‌شده از ENCRYPTION_KEY — در هر فراخوانی محاسبه می‌شود
    (sha256 ارزان است؛ cache نه، تا تست‌ها بتوانند کلید را عوض کنند)."""
    digest = hashlib.sha256(settings.ENCRYPTION_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> bytes:
    """plaintext → ciphertext (bytes) برای ذخیره در ستون bytea."""
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_secret(ciphertext: bytes) -> str:
    """ciphertext (bytes) → plaintext — فقط داخل مسیر connector، بعد از RBAC+RLS."""
    try:
        return _fernet().decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialTampered("credential ciphertext failed authentication") from exc
