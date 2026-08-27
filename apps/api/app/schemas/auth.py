import uuid
from typing import Optional

from pydantic import BaseModel, EmailStr


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    organization_name: Optional[str] = None  # اگر ارائه شود، سازمان جدید می‌سازد


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str

    class Config:
        from_attributes = True


class MeResponse(BaseModel):
    user: UserOut
    memberships: list["MembershipOut"]


class MembershipOut(BaseModel):
    organization_id: uuid.UUID
    organization_name: str
    organization_slug: str
    role: str

    class Config:
        from_attributes = True
