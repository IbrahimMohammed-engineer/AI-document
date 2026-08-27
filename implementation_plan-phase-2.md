# Phase 2 — Authentication, Authorization & Multi-Tenancy

## Goal

Implement the complete security substrate that every later phase depends on:
secure login, JWT access tokens, rotating refresh tokens, RBAC with four-layer enforcement, audit logging, Redis-based login rate limiting, password reset, and the frontend login/register UI.

## Open Questions

> [!IMPORTANT]
> **Email delivery for password reset:** For Phase 2, the reset token is returned in the API response body (dev mode only). A real email provider (SendGrid/Mailgun/SES) is needed for production. Should we integrate one now, or defer to Phase 16 (Security Hardening)?

> [!NOTE]
> **Invite-based registration:** Phase 2 implements the "org creation + first admin" path only. Invite tokens (for adding more users to an existing org) are a small addition — include now, or defer to Phase 3 when the user management UI is built?

---

## Proposed Changes

---

### Backend: Domain Layer

#### [NEW] [permissions.py](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/domain/permissions.py)
Pure Python permission evaluation — resolves a user's effective permissions from their roles. Zero framework imports — fully unit-testable.
- `PermissionKey` str-enum of all 9 permission keys (matching the seeded catalog)
- `has_permission(user_roles, key) → bool`
- `get_user_permissions(user_roles) → set[str]`

---

### Backend: Service Layer

#### [NEW] [authorization_service.py](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/authorization_service.py)
`AuthorizationService` — resource-level checks called by every domain service:
- `check_permission(user, permission_key, db)` — re-queries DB for current role state (not trusting JWT claims); raises `InsufficientPermissionsError`
- `resolve_allowed_document_filter(user)` → predicate that Phase 3+ queries use for visibility rules

#### [NEW] [auth_service.py](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/auth_service.py)
`AuthService` — owns all auth use cases:
- `register(org_name, slug, email, full_name, password, db, redis)` → creates org + first Admin user + issues token pair
- `login(email, password, org_id, db, redis, request)` → Argon2 verify → token pair → audit `USER_LOGIN`; generic 401 on any failure
- `refresh_tokens(raw_refresh_token, db, redis)` → rotate refresh token (invalidates old), issue new access token; reuse of revoked token triggers full session revocation
- `logout(raw_refresh_token, db)` → revoke refresh record → audit `LOGOUT`
- `get_current_user(token, db)` → decode JWT, load `User` + `Organization` + roles from DB
- `request_password_reset(email, org_id, db, redis)` → generate reset token (stored in Redis, TTL 1h)
- `reset_password(reset_token, new_password, db, redis)` → verify + consume token (atomic), update hash, revoke all refresh tokens

#### [NEW] [audit_logger.py](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/audit_logger.py)
`AuditLogger` — the sole write path to `audit_logs`. INSERT-only (no UPDATE/DELETE), matching the DB-level grant restriction from Migration 003:
- `log(db, organization_id, user_id, action, resource_type, resource_id, metadata, request)`
- Audit action constants: `USER_LOGIN`, `USER_CREATED`, `LOGIN_FAILED`, `LOGOUT`, `PASSWORD_RESET_REQUESTED`, `PASSWORD_RESET_COMPLETED`, `PERMISSION_CHANGED`, `TOKEN_REVOKED_REUSE_DETECTED`

---

### Backend: Repository Layer

#### [NEW] [refresh_token_repository.py](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/refresh_token_repository.py)
`RefreshTokenRepository`:
- `create(user_id, token_hash, expires_at, user_agent, ip_address)` → RefreshToken
- `get_by_token_hash(token_hash)` → RefreshToken | None
- `revoke(token_id)` → sets `revoked_at`
- `revoke_all_for_user(user_id)` → for password reset + theft detection
- `delete_expired()` → cleanup job (called from maintenance, Phase 4+)

#### [MODIFY] [user_repository.py](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/user_repository.py)
Add:
- `get_with_roles(user_id, org_id)` — eager-loads roles + permissions for auth resolution
- `update_last_login(user_id)` — sets `users.last_login_at`
- `update_password_hash(user_id, org_id, new_hash)` — used by password reset

---

### Backend: Infrastructure Layer

#### [NEW] [rate_limiter.py](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/infrastructure/rate_limiter.py)
Redis-based sliding-window rate limiter:
- `check_and_increment(redis, key, limit, window_seconds)` → raises `RateLimitExceededError` if exceeded, returns remaining attempts
- Applied on `POST /auth/login` at 5 attempts/minute/IP
- Reusable by Phase 16 for general API rate limiting

#### [NEW] [password_reset_store.py](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/infrastructure/password_reset_store.py)
Redis-backed, TTL-keyed password reset token store:
- `store(redis, token_hash, payload_json, ttl_seconds=3600)` → stores `{user_id, org_id}`
- `consume(redis, token_hash)` → atomically retrieves and deletes (single-use via GETDEL); returns payload | None

---

### Backend: API Layer

#### [MODIFY] [deps.py](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/api/deps.py)
Implement the full dependency chain (replacing Phase 1 stubs):
- `get_current_user(credentials, db)` → resolves `User` + `Organization` from JWT; raises `401` on any failure
- `require_permission(key)` → factory returning a FastAPI dep that calls `AuthorizationService.check_permission` (live DB re-check)

#### [NEW] [api/auth.py](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/api/auth.py)
Auth router (`/auth` prefix):
- `POST /auth/register` — creates org + first admin user
- `POST /auth/login` — rate-limited (Redis); returns `{ accessToken, expiresIn }` + HttpOnly Secure SameSite=Strict refresh cookie
- `POST /auth/refresh` — rotates refresh token
- `POST /auth/logout` — revokes refresh token
- `GET /auth/me` — returns current user + org + resolved permissions (advisory for UI)
- `POST /auth/forgot-password` — stores reset token in Redis; returns token in response body (dev mode)
- `POST /auth/reset-password` — validates and consumes reset token, updates password

#### [NEW] [schemas/auth.py](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/schemas/auth.py)
Pydantic v2 request/response models:
- `RegisterRequest` — `org_name, slug, email, full_name, password`
- `LoginRequest` — `email, password, org_slug`
- `TokenResponse` — `access_token, expires_in, token_type`
- `UserMeResponse` — `id, email, full_name, organization, permissions[]`
- `ForgotPasswordRequest` — `email, org_slug`
- `ResetPasswordRequest` — `token, new_password`

---

### Backend: Tests

#### [NEW] tests/unit/test_permissions.py
Unit tests for `domain/permissions.py` — all 3 system roles × all 9 permission keys; edge cases (no roles, multiple roles)

#### [NEW] tests/unit/test_security.py
Unit tests for `core/security.py` — hash/verify round-trip; JWT sign/verify; expired token raises `TokenExpiredError`; wrong-type token raises `TokenInvalidError`

#### [NEW] tests/api/test_auth.py
Full API test suite over httpx ASGI client (testcontainers: real PG + Redis):
- Register → Login → `/me` → Refresh → Logout round-trip
- Generic `401` on wrong password (user enumeration prevention — same error for wrong password and unknown email)
- Rate limit: 5th failed login in a window → `429`
- Refresh token rotation: original token rejected after first rotation
- Refresh token reuse detection: reused rotated token → ALL tokens for user revoked
- `require_permission` `403` matrix parametrized over guarded routes
- Cross-org access attempt → `403/404` (never returns data)
- Password reset: request → consume (single-use) → verify old password rejected, new works

---

### Frontend: Auth UI

#### [NEW] src/features/auth/LoginPage.tsx
Centered `AuthCard`, email + password fields, inline blur validation, generic error banner (no user enumeration), rate-limit retry countdown, SSO placeholder button, link to register

#### [NEW] src/features/auth/RegisterPage.tsx
Org name + slug, full name, email, password with strength meter, ToS acceptance checkbox, links to login

#### [NEW] src/features/auth/ForgotPasswordPage.tsx + ResetPasswordPage.tsx
Email entry → confirmation banner → token-from-URL reset form

#### [NEW] src/features/auth/AuthCard.tsx + PasswordInput.tsx
Shared card shell component; password field with show/hide toggle

#### [NEW] src/store/authStore.ts
Zustand auth store:
- `accessToken` — in memory only (never `localStorage`)
- `currentUser` — `UserMeResponse` | null
- Actions: `login(email, password, orgSlug)`, `logout()`, `refreshAccessToken()`
- Computed: `isAuthenticated`

#### [NEW] src/lib/api/auth.ts
Typed API functions: `loginApi`, `registerApi`, `refreshApi`, `logoutApi`, `getMeApi`, `forgotPasswordApi`, `resetPasswordApi`

#### [MODIFY] src/App.tsx
- Add `PrivateRoute` wrapper: unauthenticated → `/login?reason=expired`
- Wire `LoginPage`, `RegisterPage`, `ForgotPasswordPage`, `ResetPasswordPage` into `/login`, `/register`, `/forgot-password`, `/reset-password` routes
- Axios 401 interceptor in `client.ts`: call `authStore.refreshAccessToken()` → retry once → redirect to `/login?reason=expired` on second failure

---

## Security Properties

| Property | Mechanism |
|---|---|
| No user enumeration on login | Generic 401 on wrong password AND unknown email; argon2 verify runs either way (constant-time path) |
| JWT claims advisory only | `get_current_user` re-loads User from DB; `require_permission` re-queries roles from DB |
| Refresh token theft detection | Rotation on every use; reused revoked token → revoke ALL tokens for user + `TOKEN_REVOKED_REUSE_DETECTED` audit event |
| No tokens in localStorage | Access token: Zustand in-memory; Refresh: HttpOnly Secure SameSite=Strict cookie |
| Login rate limiting | 5 attempts / IP / 60s via Redis sliding window → `429` with `Retry-After` header |
| Password reset single-use | Redis `GETDEL` (atomic consume) — token vanishes on first use |
| Audit trail | `USER_LOGIN`, `LOGIN_FAILED`, `USER_CREATED`, `LOGOUT`, `TOKEN_REVOKED_REUSE_DETECTED` written to immutable `audit_logs` |

## Verification Plan

### Automated Tests
```bash
# Unit tests (no Docker needed)
pytest tests/unit/test_permissions.py tests/unit/test_security.py -v

# API integration tests (testcontainers: real PostgreSQL + Redis)
pytest tests/api/test_auth.py -v -m integration
```

### Manual Verification
1. `docker compose up --build`
2. `docker compose exec backend alembic upgrade head`
3. Register: `POST /auth/register` — verify org + user created, token returned
4. Login → receive `accessToken` + refresh cookie; call `GET /auth/me`
5. Refresh → new `accessToken`; confirm old refresh token rejected on retry
6. Logout → refresh cookie rejected on next `/auth/refresh`
7. 5 failed logins in < 60s → `429` with countdown
8. Open `http://localhost:5173/login` in browser; complete login flow
9. Inspect `audit_logs` table: `USER_LOGIN`, `LOGIN_FAILED`, `USER_CREATED` rows present
