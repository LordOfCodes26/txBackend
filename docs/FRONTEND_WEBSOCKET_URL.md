# Live updates: the WebSocket address the browser uses

For: the Next.js app in `/root/frontend`. **Fixed** in frontend commit `a024470`
(2026-10-02); this note explains the problem and the fix.

## The problem (before the fix)

The pages that update live (occupancy board, the till screen's `card_tapped`, the deposit
desk) built the browser's WebSocket address from `API_URL`:

```ts
const socketBase = getApiUrl().replace(/^http/, "ws");
```

`API_URL` is where the **Next.js server** reaches Django. On the offline server it is a
local-only address, `http://127.0.0.1:8001`, so every browser was told to connect to
`ws://127.0.0.1:8001/ws/...`: its **own** computer. Live updates never connected.

## The fix

`src/lib/socket-url.ts`, `getSocketBase()`, used by the three server pages that render
those components:

1. `WS_URL` (server-side setting, read at run time) when set: an explicit override.
2. In **production**: the address the browser loaded the page from, taken from the request
   (`Host` / `X-Forwarded-Host` and `X-Forwarded-Proto`, set by nginx), e.g.
   `wss://192.168.1.10`. nginx sends `/ws/` on that address to Django.
3. In **development**: `API_URL` as before (the staging backend, or the offline development
   backend `http://127.0.0.1:8000`, which `uvicorn` serves with WebSockets).

Checked on a fresh offline install: the page gives `wss://<server address>`, and a
connection through nginx opens and receives the first message.

## When to set `WS_URL`

Only when the default is wrong, e.g. the offline development copy opened **directly** from
another PC (`npm run dev -- -H 0.0.0.0`): the browser must reach the development backend,
so put `WS_URL=ws://<server-ip>:8000` in `~/frontend-dev/.env.local` and restart
`npm run dev`. Through the SSH tunnel (README) nothing needs setting.
