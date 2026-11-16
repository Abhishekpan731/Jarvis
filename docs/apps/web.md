# JARVIS web app

**Author**: Abhishek Pandey
**Last Updated**: 2026-11-16

`apps/web` is the only user interface. It is a Next.js 15.5.29 App Router
app. The browser talks to the API for JARVIS data and does not open a database
or call model and Google APIs directly. Google OAuth setup redirects the
browser to Google's consent screen.

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

Requests through the shared API helper send `credentials: "include"`; the streaming request does not send cookies. The access token lives in
`sessionStorage` under `jarvis_access` and is sent as
`Authorization: Bearer`. The refresh token is an HttpOnly cookie scoped to
`/api/v1/auth`, but the web API client has no refresh method and does not
renew access tokens automatically. Closing the tab clears the access token;
the persistent refresh cookie does not restore the session automatically. State-changing routes still require the Bearer token.

The page is one workspace: sign in or register, then a conversation list and
a streaming chat. Voice uses browser Web Speech recognition and speech
synthesis. Hold the microphone to talk, release to stop. English and Hindi
are the supported locales. If the browser cannot capture speech, the control
is absent and the text field is the path. Typed messages are not spoken. There
is no wake word.

Speech recognition is browser/provider-dependent. Some browsers may send audio
to a remote recognition service; JARVIS cannot guarantee that recognition
stays on-device. See [MDN's SpeechRecognition documentation](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition).
Speech synthesis runs in the browser.

## Security headers

`next.config.ts` sets `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a CSP whose
`connect-src` allows `'self'` and `http://localhost:8000`.
`frame-ancestors` is `'none'`. The local kind web port `3001` cannot reach
the API on `8001` with the checked-in CSP and build-time default API URL; the
web image needs both a compatible CSP and a rebuild configured for that API
origin.

## Dependency pins

`package.json` overrides keep `minimatch` at 11.0.8 for
`@typescript-eslint/typescript-estree` and `postcss` at 8.5.33. Next is pinned
at 15.5.29, which the [November 2026 Next.js security release](https://nextjs.org/blog/november-2026-security-release)
lists as the patched 15.x Maintenance LTS version. Before changing the pin,
check current advisories and deployment compatibility; do not silence a
Next.js vulnerability with an override instead of applying the vendor's patch.

## What this app does not do

- It does not store conversations, tasks, or memories. Those live on the API.
- The browser does not call model or Google APIs directly. Google OAuth
  connection setup redirects to Google's consent screen; API calls use the
  JARVIS API.
- It does not install anything on the device.
