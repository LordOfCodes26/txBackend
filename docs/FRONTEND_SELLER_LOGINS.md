# Store logins: linking SELLER users to stores

For: the Next.js app in `/root/frontend`. Goal: an admin links a user who has the
**SELLER** role to a store (seller), and optionally a position manager to a sell
position. That user then sees and manages **only that store** (goods, stock, till,
bookings, payouts).

Most of it already exists: `/sellers/[id]` has a "Linked user" dropdown and
`/positions/[id]` has a "Position manager" dropdown. This guide fixes four gaps:

1. The dropdowns list **every** user; they must list only users with the SELLER role.
2. The store form can't **unlink** a login (an empty choice sends nothing instead of `null`).
3. The store page and the store list don't show **which login** is linked.
4. Seller menus must not depend on the SELLER role having permissions.

---

## Backend behaviour (already deployed)

| Method | Path | Who | Body |
|---|---|---|---|
| GET | `/api/v1/users/?role=SELLER&ordering=full_name` | `user.view` | Candidates for store logins |
| PATCH | `/api/v1/sellers/{id}/` | `seller.update` (BOSS) | `{"user": <user id>}` links, `{"user": null}` unlinks |
| PATCH | `/api/v1/service-positions/{id}/` | `seller.update`, or the store's owner login | `{"manager": <user id>}` / `{"manager": null}` |

Each seller now also has `user_email` (read-only). Rules the backend enforces, shown as
field errors on `user` / `manager` (status 400, `VALIDATION_ERROR`):

| Message | When |
|---|---|
| "This user doesn't have the SELLER role." | The chosen user lacks the role (or is inactive) |
| "This user is already linked to another seller." | One login runs one store |
| "This user manages a sell position and can't also own a seller." | Owner vs position manager |
| "This user is a seller's owner and can't also manage a position." | The reverse |

A login acts for a store only while it has **both** the SELLER role and the link.

## 1. Regenerate the API types

`Seller` gained `user_email`. With the staging backend running:

```bash
npm run generate:api
```

## 2. `src/lib/choices.ts`: a SELLER-only user list

Add next to `userChoices`:

```ts
/** Users with the SELLER role: the only ones who can run a store or a position. */
export function sellerUserChoices(token: string) {
  return asChoices<Named>(token, "/api/v1/users/?role=SELLER&ordering=full_name", (row) =>
    row.full_name ? `${row.full_name} · ${row.email ?? ""}` : (row.email ?? row.full_name),
  );
}
```

## 3. Store pages: `src/app/(console)/sellers/[id]/page.tsx` and `sellers/new/page.tsx`

In both files:

1. Import and use the new list:

   ```ts
   import { sellerUserChoices } from "@/lib/choices";
   // [id]:  const users = manage ? await sellerUserChoices(loaded.session.token) : [];
   // new:   const users = await sellerUserChoices(session.token);
   ```

2. Rename the field so admins know what it is, and keep an empty option for "no login":

   ```tsx
   {
     name: "user",
     label: "Store login (SELLER role)",
     type: "select",
     options: [{ value: "", label: "No login" }, ...users],
     defaultValue: seller.user ? String(seller.user) : "",   // [id] page only
   },
   ```

3. `[id]` page, read-only view (`<Facts>` for users without `seller` rights): add the login.

   ```tsx
   { label: "Store login", value: show(seller.user_email) },
   ```

4. `[id]` page, header: show the login under the name, so it's visible at a glance.

   ```tsx
   <p className="text-muted-foreground text-sm">
     {seller.user_email ? `Store login: ${seller.user_email}` : "No store login yet"} ·
     Updated {showTime(seller.updated_at)}
   </p>
   ```

Field errors from the backend (e.g. "This user doesn't have the SELLER role.") already
appear under the dropdown: `FieldForm` shows `state.fields.user`.

## 4. `src/app/(console)/mutations.ts`: allow unlinking

In `updateSeller`, send `null` when "No login" is chosen (same pattern as `updatePosition`
uses for `manager`):

```ts
user: text(formData, "user") ? optionalInt(formData, "user") : null,
```

Leave `createSeller` as it is (no login on create simply means none).

## 5. Position pages: `src/app/(console)/positions/[id]/page.tsx` and `positions/new/page.tsx`

Replace `userChoices` with `sellerUserChoices` for the **Position manager** dropdown,
and add the empty option:

```tsx
{
  name: "manager",
  label: "Position manager (SELLER role)",
  type: "select",
  options: [{ value: "", label: "No manager" }, ...users],
  defaultValue: position.manager ? String(position.manager) : "",
},
```

`updatePosition` already sends `null` for an empty choice.

## 6. Store list: `src/app/(console)/sellers/page.tsx`

Show the login as a column, so admins see which stores still need one:

```tsx
headers={["Name", "Store login", "Contact", "Email", "Phone", "Status"]}
rows={data.results.map((seller) => [
  seller.name,
  show(seller.user_email),
  show(seller.contact_name),
  show(seller.email),
  show(seller.phone),
  show(seller.status),
])}
```

Optional filter for "stores without a login": `/api/v1/sellers/?has_user=false`.

## 7. Korean labels: `src/lib/ko-ui.ts`

Add the new labels to the dictionary (your file's existing style):

```ts
"Store login (SELLER role)": "매장 로그인 (SELLER 역할)",
"Store login": "매장 로그인",
"No login": "로그인 없음",
"No store login yet": "아직 매장 로그인이 없습니다",
"Position manager (SELLER role)": "매대 책임자 (SELLER 역할)",
"No manager": "책임자 없음",
```

## 8. Seller menus: don't rely on permissions

The SELLER role must have **no permissions** (any permission on it applies to every
store, e.g. `good.view` shows all stores' goods). So a seller's `permissions` list is
empty, and menus for sellers must come from the store link instead:

```ts
// e.g. in the console layout / navigation, server side
const seller = await djangoFetch<Seller | null>("/api/v1/sellers/me/", { accessToken })
  .catch(() => null);           // 404 SELLER_PROFILE_NOT_FOUND = not a seller
const isSeller = seller !== null && seller.status === "ACTIVE";
// show Goods, Stock, Till, Bookings, Payouts when isSeller (the API narrows them to the store)
```

Check `canManage(...)` in `src/lib/current-user.ts`: if it only looks at permissions,
seller pages (goods, stock, till) must also accept `isSeller`. Staff checks (`seller`,
`good`, `purchase` permissions) stay as they are.

## 9. Try it on staging

1. In the Django admin (Accounts → Roles → SELLER), make sure the role has **no**
   permissions.
2. Users → pick a user → give them the **SELLER** role.
3. Sellers → open a store → **Store login** → choose that user → Save. The header shows
   "Store login: …".
4. Choose a user **without** the role → Save: the error "This user doesn't have the SELLER
   role." appears under the field.
5. Sign in as the store login: Goods, Till and Bookings show only that store.
6. Back as admin: choose **No login** → Save: the user loses access to the store.
