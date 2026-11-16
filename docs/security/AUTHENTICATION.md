# JARVIS — Authentication

**Author**: Abhishek Pandey
**Last Updated**: 2026-11-16

Complements [SECURITY.md](SECURITY.md) with the behaviour that is implemented.

## Tokens

| Token | Lifetime | Where it lives |
|---|---|---|
| Access token, JWT HS256 | 900 seconds | `Authorization: Bearer` header. The web app keeps it in `sessionStorage`, so it dies with the tab. |
| Refresh token, 48 random bytes | 30 days | `jarvis_refresh` cookie. HttpOnly, SameSite=Lax, path restricted to `/api/v1/auth`. `Secure` in production. |

The database stores the SHA-256 hash of the refresh token. The raw value is
only in the cookie.

## Rotation

`POST /api/v1/auth/refresh` issues a new refresh token and marks the presented
one as replaced. Presenting a replaced token revokes every session in its
family and commits that revocation before responding, so the family is dead
even though the request itself failed.

## Passwords

Argon2id through `argon2-cffi`. Login failures return the same message for an
unknown email and a wrong password. Eight failed attempts from one IP in five
minutes return 429. The counter is in memory and resets on restart.

## Account deletion

`DELETE /api/v1/auth/account` sets `deleted_at` and revokes every session for
the account, so a token issued before deletion stops working immediately. The
account can no longer log in. Rows are not yet erased; that is outstanding
work.

Changing a password also revokes every session. The caller must sign in again.

## What is not enforced yet

Row-level security policies exist, but the application connects as the table
owner, which bypasses them. Isolation currently depends on every query
filtering on `user_id`. A test confirms one user cannot read another's
conversation. Forcing RLS for the application role is outstanding.
