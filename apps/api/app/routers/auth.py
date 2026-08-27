import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.middleware.tenant import get_current_user
from app.models.membership import Membership, RoleEnum
from app.models.organization import Organization
from app.models.user import User
from app.schemas.auth import LoginRequest, MeResponse, MembershipOut, RegisterRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


def slugify(name: str) -> str:
    s = re.sub(r"[^\w]+", "-", name.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "org"


@router.post("/register", response_model=TokenResponse)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="ایمیل قبلاً ثبت شده است")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="رمز عبور باید حداقل ۸ کاراکتر باشد")

    user = User(email=payload.email, full_name=payload.full_name, hashed_password=hash_password(payload.password))
    db.add(user)
    db.flush()

    org_id = None
    if payload.organization_name:
        base_slug = slugify(payload.organization_name)
        slug = base_slug
        i = 1
        while db.query(Organization).filter(Organization.slug == slug).first():
            slug = f"{base_slug}-{i}"
            i += 1
        org = Organization(name=payload.organization_name, slug=slug)
        db.add(org)
        db.flush()
        org_id = org.id
        membership = Membership(user_id=user.id, organization_id=org.id, role=RoleEnum.owner)
        db.add(membership)

    db.commit()

    token = create_access_token({"sub": str(user.id), "org_id": str(org_id) if org_id else None})
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="ایمیل یا رمز عبور نادرست است")

    # آخرین عضویت را به عنوان org_id پیش‌فرض در توکن قرار بده
    membership = db.query(Membership).filter(Membership.user_id == user.id).first()
    org_id = str(membership.organization_id) if membership else None
    token = create_access_token({"sub": str(user.id), "org_id": org_id})
    return TokenResponse(access_token=token)


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    memberships = db.query(Membership).filter(Membership.user_id == user.id).all()
    out = []
    for m in memberships:
        org = db.get(Organization, m.organization_id)
        out.append(MembershipOut(
            organization_id=m.organization_id,
            organization_name=org.name if org else "",
            organization_slug=org.slug if org else "",
            role=m.role.value,
        ))
    return MeResponse(user=UserOut.model_validate(user), memberships=out)


@router.post("/switch-organization", response_model=TokenResponse)
def switch_organization(organization_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    membership = db.query(Membership).filter(Membership.user_id == user.id, Membership.organization_id == organization_id).first()
    if not membership:
        raise HTTPException(status_code=403, detail="عضو این سازمان نیستید")
    token = create_access_token({"sub": str(user.id), "org_id": str(organization_id)})
    return TokenResponse(access_token=token)
