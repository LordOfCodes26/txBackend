# Adding a "Simulate tap" button to the till screen

For: the Next.js app in `/root/frontend`. Goal: test the purchase flow on staging without a
physical card reader. The button triggers a real tap through the backend; your existing
`Checkout` component then reacts exactly as it does to a real reader: it gets `card_tapped`
on the WebSocket (or sees `presented_card` on refresh) and switches to the PIN step.

**Production safety, three layers:**

1. The button only renders when the **server-only** env var `TAP_SIMULATOR=true` is set
   (only in `.env.development` / staging, never in `.env.production`).
2. The server actions refuse to run without that variable.
3. The backend endpoints answer `404` unless the backend itself has `TEST_CONSOLE_ENABLED`
   (staging only).

Backend endpoints used (see `FRONTEND_README.md`, "Simulating card taps"):

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/v1/test-console/cards/` | `?search=` | `[{developer, employee_number, full_name, developer_status, card_uid, card_status, balance, account_status, has_pin}]` |
| POST | `/api/v1/test-console/simulate-tap/` | `{purchase, developer}` or `{purchase, uid}` | the reader's reply: `{result, accepted, display_message, developer, purchase}` |

---

## 1. Environment flag

`.env.development` (and the staging environment), **not** `.env.production`:

```bash
TAP_SIMULATOR=true
```

It's read only on the server (no `NEXT_PUBLIC_` prefix), so it never reaches the browser bundle.

## 2. Server actions: `src/app/(console)/mutations.ts`

Add these next to `developerBalance` / `detectedReaders`. They follow the same pattern
(`getSession()` + `djangoFetch` with `accessToken`):

```ts
export type TestCard = {
  developer: number;
  employee_number: string;
  full_name: string;
  developer_status: string;
  card_uid: string;
  card_status: string;
  balance: string | null;
  account_status: string | null;
  has_pin: boolean;
};

export type SimulatedTap =
  | {
      ok: true;
      result: string;
      accepted: boolean;
      message: string;
      purchase: number | null;
    }
  | { ok: false; error: string };

function tapSimulatorEnabled() {
  return process.env.TAP_SIMULATOR === "true";
}

export async function testCards(search: string): Promise<TestCard[]> {
  if (!tapSimulatorEnabled()) return [];
  const session = await getSession();
  if (!session) return [];
  try {
    return await djangoFetch<TestCard[]>(
      `/api/v1/test-console/cards/?search=${encodeURIComponent(search)}`,
      { accessToken: session.token },
    );
  } catch {
    return [];
  }
}

export async function simulateTap(
  purchaseId: number,
  who: { developer: number } | { uid: string },
): Promise<SimulatedTap> {
  if (!tapSimulatorEnabled()) return { ok: false, error: "The tap simulator is disabled." };
  const session = await getSession();
  if (!session) return { ok: false, error: "Not signed in." };
  try {
    const reply = await djangoFetch<{
      result: string;
      accepted: boolean;
      display_message: string;
      purchase: number | null;
    }>("/api/v1/test-console/simulate-tap/", {
      method: "POST",
      accessToken: session.token,
      body: JSON.stringify({ purchase: purchaseId, ...who }),
    });
    return {
      ok: true,
      result: reply.result,
      accepted: reply.accepted,
      message: reply.display_message,
      purchase: reply.purchase,
    };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof DjangoError ? error.message : "Could not simulate the tap.",
    };
  }
}
```

Add `DjangoError` to the existing import: `import { DjangoError, djangoFetch } from "@/lib/django";`.

## 3. The button component: `src/app/(console)/purchases/tap-simulator.tsx`

```tsx
"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { simulateTap, testCards, type TestCard } from "@/app/(console)/mutations";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function TapSimulator({ purchaseId }: { purchaseId: number }) {
  const router = useRouter();
  const [search, setSearch] = useState("");
  const [cards, setCards] = useState<TestCard[]>([]);
  const [developer, setDeveloper] = useState<number | null>(null);
  const [uid, setUid] = useState("");
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [pending, startTransition] = useTransition();

  // Debounced search for test developers.
  useEffect(() => {
    const timer = window.setTimeout(async () => {
      const rows = await testCards(search.trim());
      setCards(rows);
      setDeveloper((current) =>
        rows.some((row) => row.developer === current) ? current : rows[0]?.developer ?? null,
      );
    }, 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  function tap() {
    const who = uid.trim() ? { uid: uid.trim() } : developer ? { developer } : null;
    if (!who) return setNote({ ok: false, text: "Choose a developer or enter a card UID." });
    startTransition(async () => {
      const reply = await simulateTap(purchaseId, who);
      if (!reply.ok) return setNote({ ok: false, text: reply.error });
      setNote({ ok: reply.accepted, text: `${reply.result}: ${reply.message}` });
      // The WebSocket usually updates the screen first; this covers polling-only cases.
      router.refresh();
    });
  }

  return (
    <Card className="border-dashed border-amber-500">
      <CardHeader>
        <CardTitle>Simulate tap (testing only)</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3">
        <div className="grid gap-1">
          <Label htmlFor="sim-search">Find a test developer</Label>
          <Input
            id="sim-search"
            placeholder="Name or employee number"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            autoComplete="off"
          />
        </div>
        <div className="grid gap-1">
          <Label htmlFor="sim-developer">Developer paying</Label>
          <select
            id="sim-developer"
            className="border-input bg-background h-9 rounded-md border px-3 text-sm"
            value={developer ?? ""}
            onChange={(event) => setDeveloper(Number(event.target.value) || null)}
          >
            {cards.length === 0 ? <option value="">No matching developers</option> : null}
            {cards.map((card) => (
              <option key={card.developer} value={card.developer}>
                {card.full_name} ({card.employee_number}) · {card.balance ?? "-"} ·{" "}
                {card.has_pin ? "PIN ✓" : "no PIN"}
                {card.card_status !== "ACTIVE" ? ` · card ${card.card_status}` : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="grid gap-1">
          <Label htmlFor="sim-uid">…or any card UID (e.g. an unknown card)</Label>
          <Input
            id="sim-uid"
            placeholder="04FFFFFF"
            value={uid}
            onChange={(event) => setUid(event.target.value)}
            autoComplete="off"
          />
        </div>
        <Button type="button" variant="outline" onClick={tap} disabled={pending}>
          {pending ? "Tapping…" : "Simulate tap"}
        </Button>
        {note ? (
          <p className={note.ok ? "text-sm text-emerald-600" : "text-destructive text-sm"}>{note.text}</p>
        ) : null}
      </CardContent>
    </Card>
  );
}
```

## 4. Show it while the till waits for a card

**`src/app/(console)/purchases/[id]/page.tsx`** (server component): pass the flag down.

```tsx
<Checkout
  purchaseId={purchase.id}
  positionId={purchase.service_position}
  socketBase={socketBase}
  items={purchase.items}
  total={purchase.total}
  currency={purchase.currency}
  presented={presented}
  simulator={process.env.TAP_SIMULATOR === "true"}
/>
```

**`src/app/(console)/purchases/checkout.tsx`**:

1. Import it: `import { TapSimulator } from "./tap-simulator";`
2. Add the prop: `simulator = false,` to the destructured props and `simulator?: boolean;` to
   the props type.
3. In the `mode === "waiting" || !developer` branch, render it under the waiting card:

```tsx
if (mode === "waiting" || !developer) {
  return (
    <div className="grid gap-4">
      <Card>
        {/* … existing "Waiting for the card" card, unchanged … */}
      </Card>
      {simulator ? <TapSimulator purchaseId={purchaseId} /> : null}
    </div>
  );
}
```

No other change is needed: the existing `card_tapped` WebSocket handler and the 1-second
`router.refresh()` polling already move the screen to the PIN step.

## 5. Try it

1. `npm run dev` with `TAP_SIMULATOR=true` in `.env.development`.
2. Sign in as `seller` (or an admin) → Purchases → open a till → add goods →
   **Scan card to buy**.
3. In **Simulate tap**, pick a developer (the list shows balance and PIN status) → **Simulate
   tap**. The screen switches to the buyer/PIN step.
4. Enter the demo PIN (ask the backend team) and confirm.

Test cases: enough balance; too little balance (`INSUFFICIENT_BALANCE`); wrong PIN 5 times
(`PIN_LOCKED`); UID `04FFFFFF` (unknown card); a developer whose card shows `BLOCKED`;
simulate a second developer before confirming (the newest tap wins).

## Notes

- The simulated tap uses the purchase's reader. If the purchase has none, it gets your
  personal simulated reader `SIM-<user id>`, which then shows as the purchase's reader. That's
  expected on staging.
- Only the purchase's own seller or staff with `purchase.create` may simulate taps on it.
- Before building for production, check `TAP_SIMULATOR` is absent from `.env.production`. Even
  if it were set, the backend endpoints return `404` in production.
