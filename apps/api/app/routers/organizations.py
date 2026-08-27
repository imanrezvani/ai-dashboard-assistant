import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.middleware.tenant import get_current_organization_id, get_current_user, require_membership
from app.models.membership import Membership, RoleEnum, ROLE_HIERARCHY
from app.models.organization import Organization
from app.models.user import User
from app.schemas.organization import InviteRequest, MemberOut, MemberRoleUpdate, OrganizationCreate, OrganizationOut

router = APIRouter(prefix="/organizations", tags=["organizations"])


def slugify(name: str) -> str:
    s = re.sub(r"[^\w]+", "-", name.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "org"


@router.post("", response_model=OrganizationOut)
def create_organization(payload: OrganizationCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # اگر slug تکراری بود
    if db.query(Organization).filter(Organization.slug == payload.slug).first():
        raise HTTPException(status_code=400, detail="شناسه سازمان تکراری است")
    org = Organization(name=payload.name, slug=payload.slug)
    db.add(org)
    db.flush()
    membership = Membership(user_id=user.id, organization_id=org.id, role=RoleEnum.owner)
    db.add(membership)
    db.commit()
    db.refresh(org)
    return org


@router.get("", response_model=list[OrganizationOut])
def list_my_organizations(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    memberships = db.query(Membership).filter(Membership.user_id == user.id).all()
    org_ids = [m.organization_id for m in memberships]
    if not org_ids:
        return []
    return db.query(Organization).filter(Organization.id.in_(org_ids)).all()


@router.get("/{org_id}", response_model=OrganizationOut)
def get_organization(org_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_membership(org_id, db, user)
    org = db.get(Organization, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="سازمان یافت نشد")
    return org


# ---- Members ----

@router.get("/{org_id}/members", response_model=list[MemberOut])
def list_members(org_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_membership(org_id, db, user)
    rows = (
        db.query(Membership, User)
        .join(User, Membership.user_id == User.id)
        .filter(Membership.organization_id == org_id)
        .all()
    )
    return [
        MemberOut(user_id=u.id, email=u.email, full_name=u.full_name, role=m.role.value, membership_id=m.id)
        for m, u in rows
    ]


@router.post("/{org_id}/members/invite", response_model=MemberOut)
def invite_member(org_id: uuid.UUID, payload: InviteRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    caller = require_membership(org_id, db, user)
    if ROLE_HIERARCHY[caller.role] < ROLE_HIERARCHY[RoleEnum.admin]:
        raise HTTPException(status_code=403, detail="فقط Owner و Admin می‌توانند دعوت کنند")
    try:
        role = RoleEnum(payload.role)
    except ValueError:
        raise HTTPException(status_code=400, detail="نقش نامعتبر است")
    if role == RoleEnum.owner:
        raise HTTPException(status_code=400, detail="نمی‌توان مستقیماً Owner دعوت کرد")

    invited = db.query(User).filter(User.email == payload.email).first()
    if not invited:
        raise HTTPException(status_code=404, detail="کاربر با این ایمیل یافت نشد. ابتدا باید ثبت‌نام کند")

    if db.query(Membership).filter(Membership.user_id == invited.id, Membership.organization_id == org_id).first():
        raise HTTPException(status_code=400, detail="کاربر قبلاً عضو این سازمان است")

    m = Membership(user_id=invited.id, organization_id=org_id, role=role)
    db.add(m)
    db.commit()
    return MemberOut(user_id=invited.id, email=invited.email, full_name=invited.full_name, role=m.role.value, membership_id=m.id)


@router.patch("/{org_id}/members/{user_id}", response_model=MemberOut)
def update_member_role(org_id: uuid.UUID, user_id: uuid.UUID, payload: MemberRoleUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    caller = require_membership(org_id, db, current_user)
    if ROLE_HIERARCHY[caller.role] < ROLE_HIERARCHY[RoleEnum.admin]:
        raise HTTPException(status_code=403, detail="فقط Owner و Admin می‌توانند نقش را تغییر دهند")
    try:
        new_role = RoleEnum(payload.role)
    except ValueError:
        raise HTTPException(status_code=400, detail="نقش نامعتبر است")

    target = db.query(Membership).filter(Membership.user_id == user_id, Membership.organization_id == org_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="عضویت یافت نشد")
    # فقط Owner می‌تواند Owner دیگر بسازد/تغییر دهد
    if target.role == RoleEnum.owner and caller.role != RoleEnum.owner:
        raise HTTPException(status_code=403, detail="فقط Owner می‌تواند نقش Owner را تغییر دهد")
    if new_role == RoleEnum.owner and caller.role != RoleEnum.owner:
        raise HTTPException(status_code=403, detail="فقط Owner می‌تواند Owner جدید تعیین کند")

    target.role = new_role
    db.commit()
    u = db.get(User, user_id)
    return MemberOut(user_id=u.id, email=u.email, full_name=u.full_name, role=target.role.value, membership_id=target.id)


@router.delete("/{org_id}/members/{user_id}")
def remove_member(org_id: uuid.UUID, user_id: uuid.UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    caller = require_membership(org_id, db, current_user)
    if ROLE_HIERARCHY[caller.role] < ROLE_HIERARCHY[RoleEnum.admin]:
        raise HTTPException(status_code=403, detail="فقط Owner و Admin می‌توانند عضو را حذف کنند")
    target = db.query(Membership).filter(Membership.user_id == user_id, Membership.organization_id == org_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="عضویت یافت نشد")
    if target.role == RoleEnum.owner:
        # حداقل یک owner باید بماند
        owner_count = db.query(Membership).filter(Membership.organization_id == org_id, Membership.role == RoleEnum.owner).count()
        if owner_count <= 1:
            raise HTTPException(status_code=400, detail="نمی‌توان آخرین Owner را حذف کرد")
        if caller.role != RoleEnum.owner:
            raise HTTPException(status_code=403, detail="فقط Owner می‌تواند Owner را حذف کند")
    db.delete(target)
    db.commit()
    return {"ok": True}
