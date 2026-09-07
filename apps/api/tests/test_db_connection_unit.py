"""فاز ۲.۲ گام ۱ — تست‌های unit پایه DatabaseConnection + رمزنگاری (SQLite).

SQLite هیچ RLS ندارد — ایزولاسیون tenant در سطح DB (ENABLE+FORCE+Fail-Closed)
در tests/test_data_sources.py روی PostgreSQL واقعی آزموده می‌شود.
اینجا: رفتار مدل، قرارداد رمزنگاری (round-trip / tamper / نبودِ plaintext)،
فیلتر organization_id در سطح کوئری و cascade حذف سازمان.
"""

import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import _get_secret, settings
from app.core.credentials import CredentialTampered, decrypt_secret, encrypt_secret
from app.core.database import Base
from app.models import DatabaseConnection, Organization


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite به‌طور پیش‌فرض FK را اعمال نمی‌کند — برای تست cascade لازم است
    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def orgs(db):
    org_a = Organization(name="Org A", slug="dbconn-org-a")
    org_b = Organization(name="Org B", slug="dbconn-org-b")
    db.add_all([org_a, org_b])
    db.commit()
    return org_a, org_b


def _make(org_id: uuid.UUID, name: str = "conn-1", password: str = "hunter2-secret") -> DatabaseConnection:
    return DatabaseConnection(
        organization_id=org_id,
        name=name,
        engine="postgresql",
        host="db.example.internal",
        port=5432,
        database_name="analytics",
        username="bi_reader",
        encrypted_password=encrypt_secret(password),
    )


# ---------- encryption ----------

def test_encryption_round_trip_preserves_secret():
    secret = "s3cret-password-رمز-فارسی"
    ciphertext = encrypt_secret(secret)
    assert isinstance(ciphertext, bytes)
    assert ciphertext != secret.encode("utf-8")
    assert secret.encode("utf-8") not in ciphertext
    assert decrypt_secret(ciphertext) == secret


def test_tampered_ciphertext_raises_credential_tampered():
    ciphertext = bytearray(encrypt_secret("abc"))
    ciphertext[len(ciphertext) // 2] ^= 0x01
    with pytest.raises(CredentialTampered):
        decrypt_secret(bytes(ciphertext))


def test_wrong_key_cannot_decrypt(monkeypatch):
    ciphertext = encrypt_secret("abc")
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", "a-completely-different-key")
    with pytest.raises(CredentialTampered):
        decrypt_secret(ciphertext)


def test_production_requires_encryption_key(monkeypatch):
    """قرارداد production: نبودِ ENCRYPTION_KEY باید RuntimeError بدهد (مثل JWT_SECRET)."""
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError):
        _get_secret("ENCRYPTION_KEY", "dev-fallback")


# ---------- model ----------

def test_model_creation_defaults(db, orgs):
    org_a, _ = orgs
    conn = _make(org_a.id)
    db.add(conn)
    db.commit()
    assert conn.id is not None
    assert conn.enabled is True
    assert conn.status == "unknown"
    assert conn.last_checked_at is None
    assert conn.last_error is None
    assert conn.port == 5432
    assert conn.engine == "postgresql"
    assert conn.created_at is not None
    assert conn.updated_at is not None


def test_model_has_no_plaintext_attribute():
    """مدل نباید هیچ فیلد/خاصیتی به نام password داشته باشد (قرارداد API آینده)."""
    assert not hasattr(DatabaseConnection, "password")
    columns = {c.name for c in DatabaseConnection.__table__.columns}
    assert "encrypted_password" in columns
    assert "password" not in columns


def test_unique_constraint_is_per_organization(db, orgs):
    org_a, org_b = orgs
    db.add(_make(org_a.id, name="dup"))
    db.commit()
    # همان نام در همان org → خطای unique
    db.add(_make(org_a.id, name="dup"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # همان نام در org دیگر مجاز است
    db.add(_make(org_b.id, name="dup"))
    db.commit()


def test_no_plaintext_persistence(db, orgs):
    org_a, _ = orgs
    secret = "plaintext-password-must-never-be-stored"
    conn = _make(org_a.id, password=secret)
    db.add(conn)
    db.commit()
    db.expire_all()  # خواندن مجدد از DB، نه از identity map
    row = db.query(DatabaseConnection).filter(DatabaseConnection.id == conn.id).one()
    assert secret.encode("utf-8") not in (row.encrypted_password or b"")
    assert row.encrypted_password != secret.encode("utf-8")
    assert decrypt_secret(row.encrypted_password) == secret


# ---------- tenant scoping (query-level layer 3) ----------

def test_org_scoped_query_and_cascade_delete(db, orgs):
    org_a, org_b = orgs
    db.add(_make(org_a.id, name="a-conn"))
    db.add(_make(org_b.id, name="b-conn"))
    db.commit()

    ids_a = {
        c.id
        for c in db.query(DatabaseConnection)
        .filter(DatabaseConnection.organization_id == org_a.id)
        .all()
    }
    assert len(ids_a) == 1, "فیلتر organization_id فقط ردیف‌های همان org را برمی‌گرداند"

    # حذف سازمان باید اتصالاتش را هم cascade کند (FK ON DELETE CASCADE)
    db.query(Organization).filter(Organization.id == org_a.id).delete()
    db.commit()
    assert db.query(DatabaseConnection).filter(DatabaseConnection.organization_id == org_a.id).count() == 0
    assert db.query(DatabaseConnection).filter(DatabaseConnection.organization_id == org_b.id).count() == 1
