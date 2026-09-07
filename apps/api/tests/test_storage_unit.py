"""تست unit لایه FileStorage (PostgresFileStorage) — روی SQLite اجرا می‌شود.

نکته مهم: SQLite هیچ RLS ندارد و این تست‌ها هم عمداً رفتار RLS را fake نمی‌کنند.
چه چیزی اینجا آزموده می‌شود — منطق خودِ لایه storage (لایه ۳ دفاع در عمق):
  - round-trip بایت‌ها + متادیتای StoredFile
  - فیلتر اجباری organization_id در همه کوئری‌ها (load با org دیگر → FileNotFound)
  - FileNotFound برای data_source ناموجود (نه نشت اطلاعات)
  - delete() و سپس FileNotFound

چه چیزی اینجا آزموده نمی‌شود — RLS واقعی PostgreSQL (ENABLE+FORCE+Fail-Closed):
آن مسیر در tests/test_data_sources.py روی PostgreSQL واقعی پوشش داده می‌شود
(بدون DB به‌صورت module-level skip).
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.storage import FileNotFound, FileStorage, StorageError, StoredFile
from app.core.storage_postgres import PostgresFileStorage
from app.models import DataSource, Organization


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def seeded(db):
    org_a = Organization(name="Storage Org A", slug="storage-org-a")
    org_b = Organization(name="Storage Org B", slug="storage-org-b")
    db.add_all([org_a, org_b])
    db.flush()
    ds = DataSource(organization_id=org_a.id, name="unit.csv", file_type="csv", row_count=0, status="pending")
    db.add(ds)
    db.commit()
    return org_a, org_b, ds


def test_postgres_storage_implements_abstract_contract():
    storage = PostgresFileStorage()
    assert isinstance(storage, FileStorage)
    assert callable(storage.save) and callable(storage.load) and callable(storage.delete)
    assert issubclass(FileNotFound, StorageError)
    assert StoredFile.__slots__ == ("data_source_id", "organization_id", "size_bytes")


def test_save_load_roundtrip_preserves_bytes(db, seeded):
    org_a, _, ds = seeded
    storage = PostgresFileStorage()
    content = b"date,amount\n2024-01-01,10\n2024-01-02,20\n"

    stored = storage.save(db, data_source_id=ds.id, organization_id=org_a.id, content=content, content_type="text/csv")
    db.commit()

    assert isinstance(stored, StoredFile)
    assert stored.data_source_id == ds.id
    assert stored.organization_id == org_a.id
    assert stored.size_bytes == len(content)
    assert storage.load(db, data_source_id=ds.id, organization_id=org_a.id) == content


def test_load_rejects_other_organization(db, seeded):
    """فیلتر organization_id: فایل org A با org B قابل بازیابی نیست (defense-in-depth)."""
    org_a, org_b, ds = seeded
    storage = PostgresFileStorage()
    storage.save(db, data_source_id=ds.id, organization_id=org_a.id, content=b"secret,bytes\n")
    db.commit()

    with pytest.raises(FileNotFound):
        storage.load(db, data_source_id=ds.id, organization_id=org_b.id)


def test_load_unknown_data_source_raises_file_not_found(db, seeded):
    """data_source ناموجود هم FileNotFound است — بدون افشای تفاوت موجود/غیرموجود."""
    org_a, _, _ = seeded
    storage = PostgresFileStorage()
    with pytest.raises(FileNotFound):
        storage.load(db, data_source_id=uuid.uuid4(), organization_id=org_a.id)


def test_delete_removes_file(db, seeded):
    org_a, _, ds = seeded
    storage = PostgresFileStorage()
    storage.save(db, data_source_id=ds.id, organization_id=org_a.id, content=b"abc")
    db.commit()

    storage.delete(db, data_source_id=ds.id, organization_id=org_a.id)
    db.commit()

    with pytest.raises(FileNotFound):
        storage.load(db, data_source_id=ds.id, organization_id=org_a.id)
