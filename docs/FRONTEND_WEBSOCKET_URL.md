# Live updates: the WebSocket address the browser uses

For: the Next.js app in `/root/frontend`. Found on the offline test server (2026-10-02).

## The problem

The pages that update live build the browser's WebSocket address from `API_URL`:

```ts
// src/app/(console)/purchases/[id]/page.tsx, attendance/current-status.tsx, finance/deposits/page.tsx
const socketBase = getApiUrl().replace(/^http/, "ws");
```

`API_URL` is the address the **Next.js server** uses to reach Django. On the offline server
that is a local-only address, `http://127.0.0.1:8001`, so every browser is told to connect to
`ws://127.0.0.1:8001/ws/...`: its **own** computer. Live updates (occupancy board, the till
screen's `card_tapped`, the deposit desk) never connect; pages only change on reload or polling.
On staging it happened to work because `API_URL` there is the public `https://<ip>:8443`.

## The fix

The browser should connect to **the address it loaded the page from**: nginx routes `/ws/` on
that same address to the backend (`wss://<server-ip>/ws/...`). Only development needs a
different address (the backend on another port), given by an optional public setting.

1. **`src/lib/socket-url.ts`** (new):

   ```ts
   /** WebSocket base for the browser, e.g. "wss://192.168.1.10". Call it in the browser. */
   export function socketBase(): string {
     // Development: the backend runs on another port, e.g. ws://127.0.0.1:8000.
     const configured = process.env.NEXT_PUBLIC_WS_URL?.trim();
     if (configured) return configured.replace(/\/$/, "");
     // Production: the same address as the page; nginx sends /ws/ to the backend.
     const scheme = window.location.protocol === "https:" ? "wss" : "ws";
     return `${scheme}://${window.location.host}`;
   }
   ```

2. In the client components that open a WebSocket (`occupancy/board.tsx`,
   `purchases/checkout.tsx`, `finance/deposit-desk.tsx`), use it **inside the effect** that
   opens the socket (it reads `window`, so not during server rendering):

   ```ts
   import { socketBase } from "@/lib/socket-url";
   // ...
   socket = new WebSocket(`${socketBase()}/ws/counters/${positionId}/?ticket=${encodeURIComponent(body.ticket)}`);
   ```

   and remove the `socketBase` prop and the `getApiUrl().replace(/^http/, "ws")` lines in the
   server pages.

3. **Development settings:**
   - `.env.development` (staging backend): `NEXT_PUBLIC_WS_URL=wss://<staging-ip>:8443`
   - the offline development copy: `install-all.sh` already writes
     `NEXT_PUBLIC_WS_URL=ws://127.0.0.1:8000` into `~/frontend-dev/.env.local`.
   - **Never** set `NEXT_PUBLIC_WS_URL` for production builds (`.env.production`): the
     same-address default is right there.

`NEXT_PUBLIC_` values are fixed when `npm run dev` / `npm run build` starts: restart the dev
server after changing it.

## Check

On the installed system, sign in, open the occupancy board, and in the browser's developer
tools (Network → WS) the connection goes to `wss://<server-ip>/ws/occupancy/?ticket=…` with
status `101`. Through the SSH tunnel to a development copy it goes to
`ws://127.0.0.1:8000/ws/...`.
