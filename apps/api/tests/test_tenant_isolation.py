"""
تست ایزولیشن tenant — بررسی می‌کند که کاربر Org A نتواند داده Org B را ببیند.

سناریو:
  1. دو Organization (org_a, org_b) + دو کاربر (user_a, user_b) که هرکدام فقط عضو یک org هستند.
  2. با JWT کاربر A تلاش برای خواندن اعضای org B → باید 403 باشد (نه داده org B).
  3. خواندن اعضای org خودی باید 200 و فقط اعضای خودی را برگرداند.

همچنین:
  - تست اطمینان از وجود FORCE ROW LEVEL SECURITY در کد (main.py)
  - تست اطمینان از وجود SET LOCAL / set_config در tenant middleware
"""

import os

# اطمینان از اینکه قبل از import اپ، DATABASE_URL sqlite باشد تا نیاز به postgres واقعی نباشد
os.environ.setdefault("DATABASE_URL", "sqlite://")

import sys
import uuid
import pathlib

# PYTHONPATH: apps/api باید در path باشد
API_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fastapi.testclient import TestClient

# ---- ایمپورت بعد از ست کردن env ----
from app.core.database import Base, get_db
import app.core.database as db_module
from app.core.security import create_access_token, hash_password
from app.models import Membership, Organization, RoleEnum, User
from app.main import app


# ----- setup تست دیتابیس -----
# یک engine واحد برای کل تست (StaticPool تا در حافظه بماند)
test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

# patch کردن engine/session اصلی اپ تا startup هم روی همین DB کار کند
db_module.engine = test_engine
db_module.SessionLocal = TestSessionLocal

# ایجاد جداول
Base.metadata.drop_all(bind=test_engine)
Base.metadata.create_all(bind=test_engine)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)

# ----- helpers -----
def create_user_and_org(email: str, full_name: str, org_name: str, org_slug: str):
    db = TestSessionLocal()
    try:
        user = User(id=uuid.uuid4(), email=email, full_name=full_name, hashed_password=hash_password("TestPass123"))
        org = Organization(id=uuid.uuid4(), name=org_name, slug=org_slug)
        db.add(user)
        db.add(org)
        db.flush()
        membership = Membership(user_id=user.id, organization_id=org.id, role=RoleEnum.owner)
        db.add(membership)
        db.commit()
        db.refresh(user)
        db.refresh(org)
        return user, org
    finally:
        db.close()


def token_for(user: User, org: Organization) -> str:
    return create_access_token({"sub": str(user.id), "org_id": str(org.id)})


# ----- setup داده اولیه -----
user_a, org_a = create_user_and_org("alice@org-a.test", "Alice A", "Organization A", "org-a")
user_b, org_b = create_user_and_org("bob@org-b.test", "Bob B", "Organization B", "org-b")

token_a = token_for(user_a, org_a)
token_b = token_for(user_b, org_b)


# ----- تست‌ها -----

def test_cross_org_members_forbidden():
    """کاربر Org A نباید بتواند اعضای Org B را ببیند → 403"""
    resp = client.get(
        f"/organizations/{org_b.id}/members",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    # باید 403 باشد، نه 200 با داده
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"
    # اطمینان که body حاوی داده Org B نیست
    if resp.status_code == 200:
        data = resp.json()
        assert data == [] or len(data) == 0


def test_cross_org_get_org_forbidden():
    """کاربر Org A نباید جزئیات Org B را ببیند → 403"""
    resp = client.get(
        f"/organizations/{org_b.id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


def test_own_org_members_allowed():
    """کاربر هر org باید بتواند اعضای org خودش را ببیند"""
    resp_a = client.get(
        f"/organizations/{org_a.id}/members",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_a.status_code == 200, f"own org A should be 200, got {resp_a.status_code}: {resp_a.text}"
    data_a = resp_a.json()
    assert isinstance(data_a, list)
    assert len(data_a) == 1
    assert data_a[0]["email"] == "alice@org-a.test"

    resp_b = client.get(
        f"/organizations/{org_b.id}/members",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    assert len(data_b) == 1
    assert data_b[0]["email"] == "bob@org-b.test"


def test_cross_org_with_header_forbidden():
    """تلاش با هدر X-Organization-Id جعلی هم باید 403 باشد"""
    resp = client.get(
        f"/organizations/{org_b.id}/members",
        headers={
            "Authorization": f"Bearer {token_a}",
            "X-Organization-Id": str(org_b.id),
        },
    )
    assert resp.status_code == 403, f"header spoof should be 403, got {resp.status_code}: {resp.text}"


def test_rls_force_enabled_in_code():
    """پالیسی RLS باید با FORCE ROW LEVEL SECURITY فعال شده باشد"""
    main_path = API_DIR / "app" / "main.py"
    content = main_path.read_text(encoding="utf-8")
    assert "FORCE ROW LEVEL SECURITY" in content, "main.py باید شامل FORCE ROW LEVEL SECURITY باشد"
    assert "ENABLE ROW LEVEL SECURITY" in content, "main.py باید شامل ENABLE ROW LEVEL SECURITY باشد"
    # اطمینان از وجود USING و WITH CHECK
    assert "USING" in content and "WITH CHECK" in content


def test_set_local_in_middleware():
    """middleware باید SET LOCAL / set_config را اجرا کند"""
    tenant_path = API_DIR / "app" / "middleware" / "tenant.py"
    content = tenant_path.read_text(encoding="utf-8")
    # باید دستور SET LOCAL معادل set_config وجود داشته باشد
    assert "set_config('app.current_org_id'" in content or "SET LOCAL app.current_org_id" in content
    assert "def set_rls_context" in content
    assert "def require_membership" in content
    # require_membership باید set_rls_context را صدا بزند
    assert "set_rls_context(db, organization_id)" in content


def test_unauthenticated_access_denied():
    """بدون توکن نباید به هیچ org دسترسی داشت"""
    resp = client.get(f"/organizations/{org_a.id}/members")
    assert resp.status_code in (401, 403), f"unauth should be 401/403, got {resp.status_code}"
