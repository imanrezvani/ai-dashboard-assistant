import uuid
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_token
from app.models.membership import Membership
from app.models.user import User


def set_rls_context(db: Session, organization_id: uuid.UUID, user_id: uuid.UUID | None = None) -> None:
    """در ابتدای هر request/تراکنش tenant_id تأییدشده را روی session ست می‌کند.

    اجرای دقیق: `SET LOCAL app.current_org_id = '<tenant_id>'` + `SET LOCAL app.current_user_id`
    معادل SQLAlchemy:
        SELECT set_config('app.current_org_id', '<tenant_id>', true)
        SELECT set_config('app.current_user_id', '<user_id>', true)
    پارامتر سوم `true` یعنی LOCAL (فقط همین تراکنش).

    برای memberships که نیاز به fallback user دارند، هر دو ست می‌شوند.
    برای dialectهای غیر-Postgres (مثلاً SQLite) نادیده گرفته می‌شود.
    """
    try:
        bind = db.get_bind()
        if bind is not None and bind.dialect.name != "postgresql":
            return
    except Exception:
        pass
    try:
        db.execute(text("SELECT set_config('app.current_org_id', :org_id, true)"), {"org_id": str(organization_id)})
        if user_id:
            db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user_id)})
    except Exception:
        pass


def set_user_context(db: Session, user_id: uuid.UUID) -> None:
    """فقط user_id را ست می‌کند — برای لیست memberships قبل از داشتن org"""
    try:
        bind = db.get_bind()
        if bind is not None and bind.dialect.name != "postgresql":
            return
    except Exception:
        pass
    try:
        db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user_id)})
        # org را خالی بگذار تا policy fallback کار کند
        db.execute(text("SELECT set_config('app.current_org_id', '', true)"))
    except Exception:
        pass

security = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="توکن احراز هویت یافت نشد")
    payload = decode_token(credentials.credentials)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="توکن نامعتبر است")
    user_id = payload["sub"]
    user = db.get(User, uuid.UUID(user_id))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="کاربر یافت نشد")
    return user


def get_current_organization_id(
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-Id"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[uuid.UUID]:
    """tenant_id را از هدر یا JWT استخراج می‌کند"""
    if x_organization_id:
        try:
            return uuid.UUID(x_organization_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="شناسه سازمان نامعتبر")
    if credentials:
        payload = decode_token(credentials.credentials)
        if payload and "org_id" in payload and payload["org_id"]:
            try:
                return uuid.UUID(payload["org_id"])
            except ValueError:
                pass
    return None


def require_membership(
    organization_id: uuid.UUID,
    db: Session,
    user: User,
) -> Membership:
    """عضویت کاربر را تأیید کرده و SET LOCAL را ست می‌کند.

    برای حل chicken-egg RLS روی memberships (که Fail-Closed است)،
    ابتدا context را ست می‌کنیم تا query membership خود تحت RLS درست فیلتر شود.
    سپس اگر membership یافت نشود، 403 برمی‌گردانیم.
    """
    # ست کردن هر دو context قبل از query — تا memberships حتی با RLS دیده شود
    set_rls_context(db, organization_id, user.id)
    membership = (
        db.query(Membership)
        .filter(Membership.user_id == user.id, Membership.organization_id == organization_id)
        .first()
    )
    if not membership:
        raise HTTPException(status_code=403, detail="دسترسی به این سازمان را ندارید")
    return membership


def require_role(allowed_roles: list[str]):
    def dep(
        organization_id: uuid.UUID = Depends(get_current_organization_id),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        if not organization_id:
            raise HTTPException(status_code=400, detail="سازمان انتخاب نشده")
        membership = require_membership(organization_id, db, user)
        if membership.role.value not in allowed_roles:
            raise HTTPException(status_code=403, detail="سطح دسترسی کافی ندارید")
        return membership
    return dep
