import uuid
from datetime import datetime

from pydantic import BaseModel


class OrganizationCreate(BaseModel):
    name: str
    slug: str


class OrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime

    class Config:
        from_attributes = True


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: str
    membership_id: uuid.UUID


class MemberRoleUpdate(BaseModel):
    role: str  # owner,admin,manager,analyst,viewer


class InviteRequest(BaseModel):
    email: str
    role: str = "viewer"
