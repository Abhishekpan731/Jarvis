# JARVIS — Authentication

**Author**: Abhishek Pandey
**Last Updated**: 2026-11-16

Complements [SECURITY.md](SECURITY.md) with authentication behavior and its
known limits.

## Tokens and browser behavior

| Token | Lifetime | Where it lives |
|---|---|---|
| Access token, JWT HS256 | 900 seconds | Sent in `Authorization: Bearer`; the web client stores it in `sessionStorage`. |
| Refresh token, 48 random bytes | 30 days | `jarvis_refresh` cookie: HttpOnly, SameSite=Lax, path `/api/v1/auth`; Secure only when `APP_ENV=production`. |

The database stores the SHA-256 hash of the refresh token. The raw value is
only in the cookie. The web client does not automatically refresh or retry a
request when its access token expires; the client must handle the resulting
unauthorized response.

**Production blocker — JWT signing secret:** the application configuration
has a long development fallback for `JWT_SECRET`. Production startup checks
only that the configured value is at least 32 characters. If `JWT_SECRET` is
unset, the fallback is used and passes that length check. Production must not
run until configuration supplies a unique secret; startup does not fail closed
when it is missing. See `apps/api/app/config/__init__.py` and
`apps/api/app/main/__init__.py`.

## Rotation and passwords

`POST /api/v1/auth/refresh` issues a new refresh token and marks the presented
one as replaced. Reuse of a replaced token revokes every session in its family.

Passwords are hashed with Argon2id. Login failures return the same message for
an unknown email and a wrong password. Changing a password revokes every
session, so the caller must sign in again.

## Login and registration throttling

Login and registration each allow eight attempts per client IP in a
five-minute window, tracked as separate counters so failed logins do not
consume the registration budget or vice versa. The counters are in-process and
reset on restart. There is no per-user login or registration limit, and
refresh does not use this throttle. Selected task and assistant write routes
have a separate in-process per-user limit of 30 requests per minute. These
limits are not shared across application processes.

Implementation: `apps/api/app/routers/auth/__init__.py` and
`apps/api/app/limits/__init__.py`.

## Account deletion

`DELETE /api/v1/auth/account` sets `deleted_at` and revokes every session for
the account. The account can no longer log in. Rows are not yet erased; hard
deletion remains outstanding.

## Row-level security

Migration `0009` creates the non-owner runtime role `jarvis_app` and forces RLS
on these tenant tables only: `tasks`, `conversations`, `messages`, `memories`,
`jobs`, `idempotency_keys`, `devices`, `sync_events`, `integrations`,
`oauth_states`, `notifications`, `automations`, `task_steps`, and `task_events`.

RLS is not enabled for `users`, `sessions`, `user_preferences`, `agents`,
`agent_capabilities`, `audit_logs`, or `model_usage`. There is no repository
layer guarantee that all access is user-scoped; router handlers include direct
SQL. Review those queries as well as the applicable RLS policy when changing
data access.

Source: `database/migrations/versions/revisions_0007_0009/0009_rls_hardening.py`;
representative route SQL: `apps/api/app/routers/tasks/__init__.py`.
