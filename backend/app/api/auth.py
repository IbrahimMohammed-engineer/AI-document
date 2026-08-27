"""
Authentication API routes.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import CurrentUser, DbSession, get_redis_client
from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.infrastructure.rate_limiter import check_and_increment
from app.schemas.auth import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserMeResponse,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=not settings.is_development,
        samesite="strict",
        max_age=settings.jwt_refresh_token_expire_days * 24 * 60 * 60,
        path="/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key="refresh_token", path="/auth")


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    response: Response,
    request: Request,
    db: DbSession,
    redis=Depends(get_redis_client),
) -> TokenResponse:
    result = await AuthService.register(
        org_name=payload.org_name,
        slug=payload.slug,
        email=payload.email,
        full_name=payload.full_name,
        password=payload.password,
        db=db,
        redis=redis,
        request=request,
    )
    _set_refresh_cookie(response, result.refresh_token)
    return TokenResponse(access_token=result.access_token, expires_in=result.expires_in)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    response: Response,
    request: Request,
    db: DbSession,
    redis=Depends(get_redis_client),
) -> TokenResponse:
    client_ip = request.client.host if request.client else "unknown"
    await check_and_increment(redis, key=f"login-rate-limit:{client_ip}", limit=5, window_seconds=60)
    result = await AuthService.login(
        email=payload.email,
        password=payload.password,
        org_slug=payload.org_slug,
        db=db,
        redis=redis,
        request=request,
    )
    _set_refresh_cookie(response, result.refresh_token)
    return TokenResponse(access_token=result.access_token, expires_in=result.expires_in)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(
    response: Response,
    request: Request,
    db: DbSession,
    redis=Depends(get_redis_client),
) -> TokenResponse:
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise AuthenticationError()
    result = await AuthService.refresh_tokens(
        raw_refresh_token=refresh_token,
        db=db,
        redis=redis,
        request=request,
    )
    _set_refresh_cookie(response, result.refresh_token)
    return TokenResponse(access_token=result.access_token, expires_in=result.expires_in)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    request: Request,
    db: DbSession,
) -> Response:
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        await AuthService.logout(raw_refresh_token=refresh_token, db=db, request=request)
    _clear_refresh_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserMeResponse)
async def get_me(user: CurrentUser) -> UserMeResponse:
    return UserMeResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        organization=user.organization,
        permissions=sorted({permission.key for role in user.roles for permission in role.permissions}),
    )


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: DbSession,
    redis=Depends(get_redis_client),
) -> ForgotPasswordResponse:
    reset_token = await AuthService.request_password_reset(
        email=payload.email,
        org_slug=payload.org_slug,
        db=db,
        redis=redis,
        request=request,
    )
    return ForgotPasswordResponse(
        message="If the account exists, a reset link has been generated.",
        reset_token=reset_token if get_settings().is_development else None,
    )


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    db: DbSession,
    redis=Depends(get_redis_client),
) -> Response:
    await AuthService.reset_password(
        reset_token=payload.token,
        new_password=payload.new_password,
        db=db,
        redis=redis,
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
