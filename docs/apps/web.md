# JARVIS web app

**Author**: Abhishek Pandey
**Last Updated**: 2026-11-16

`apps/web` is the only user interface. It is a Next.js 15.5.29 App Router
app. The browser talks only to the API. It never opens a database, a model
provider, or an OAuth provider.

## Run

From `apps/web`:

```bash
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The API must already be listening on port 8000.
Full setup is in [INSTALLATION.md](../developer/INSTALLATION.md).

| Script | Command |
| --- | --- |
| `dev` | `next dev --turbopack` |
| `build` | `next build --turbopack` |
| `start` | `next start` |
| `lint` | `eslint --config config/eslint.config.mjs` |
| `typecheck` | `tsc --project config/tsconfig.json` |

There is no `*.test.*` suite in this package. Cross-service behaviour is
covered under `tests/`.

## How it reaches the API

`lib/api.ts` reads `NEXT_PUBLIC_API_URL` and defaults to
`http://localhost:8000`. That value is inlined at build time. The Docker
image sets it to the host-published API port, not a Docker DNS name.

Every request sends `credentials: "include"`. The access token lives in
`sessionStorage` under `jarvis_access` and is sent as
`Authorization: Bearer`. The refresh token is an HttpOnly cookie scoped to
`/api/v1/auth`. State-changing routes still require the Bearer token.

The page is one workspace: sign in or register, then a conversation list and
a streaming chat. Voice uses the browser Web Speech API. Hold the microphone
to talk, release to stop. English and Hindi are the supported locales. If the
browser cannot capture speech, the control is absent and the text field is
the path. Typed messages are not spoken. There is no wake word.

## Security headers

`next.config.ts` sets `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a CSP whose
`connect-src` allows `'self'` and `http://localhost:8000`.
`frame-ancestors` is `'none'`.

## Dependency pins

`package.json` overrides keep `minimatch` at 11.0.8 for
`@typescript-eslint/typescript-estree` and `postcss` at 8.5.33. Next stays
at 15.5.29. Do not bump Next to clear an audit finding.

## What this app does not do

- It does not store conversations, tasks, or memories. Those live on the API.
- It does not call OpenAI, Google, or a speech vendor.
- It does not install anything on the device.
