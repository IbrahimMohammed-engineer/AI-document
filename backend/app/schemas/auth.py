"""
Pydantic request and response models for auth flows.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    org_name: str = Field(min_length=2, max_length=255)
    slug: str = Field(min_length=2, max_length=255)
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=255)
    password: str = Field(min_length=8, max_length=256)

    @field_validator("slug")
    @classmethod
    def normalize_slug(cls, value: str) -> str:
        return value.strip().lower()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    org_slug: str = Field(min_length=2, max_length=255)

    @field_validator("org_slug")
    @classmethod
    def normalize_slug(cls, value: str) -> str:
        return value.strip().lower()


class TokenResponse(BaseModel):
    access_token: str
    expires_in: int
    token_type: str = "bearer"


class OrganizationResponse(BaseModel):
    id: str
    name: str
    slug: str

    model_config = ConfigDict(from_attributes=True)


class UserMeResponse(BaseModel):
    id: str
    email: str
    full_name: str
    organization: OrganizationResponse
    permissions: list[str]


class ForgotPasswordRequest(BaseModel):
    email: EmailStr
    org_slug: str = Field(min_length=2, max_length=255)

    @field_validator("org_slug")
    @classmethod
    def normalize_slug(cls, value: str) -> str:
        return value.strip().lower()


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    new_password: str = Field(min_length=8, max_length=256)


class ForgotPasswordResponse(BaseModel):
    message: str
    reset_token: str | None = None
