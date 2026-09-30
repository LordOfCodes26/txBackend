# Frontend Build Guide

How to build the Next.js frontend against this backend's REST API. The backend is
Django + Django REST Framework; the frontend only talks to it over HTTP.

- **API base:** `/api/v1/`
- **Interactive docs (Swagger):** `/api/docs/` (try every endpoint in the browser)
- **OpenAPI schema:** `/api/schema/` (YAML; add `?format=json` for JSON)
- **Staging server:** `https://<staging-host>:8443` (self-signed certificate) or `http://<staging-host>:8088`; ask the backend team for the address

The OpenAPI schema is the source of truth. If this guide and the schema ever disagree,
the schema wins, and please report the mismatch.

---

## 1. Hard constraints

1. **The production server may have no internet access.** The finished frontend must
   not load anything from the internet at runtime: no CDN scripts, no Google Fonts
   links, no external analytics or icon services. Bundle every asset. (`next/font`
   is fine, because it downloads fonts at build time and serves them locally.)
2. **The backend decides who can do what.** Hiding a button is only for
   convenience; every request is checked again on the server. Don't build business
   rules (balances, card state, attendance rules) into the frontend.
3. **All times are UTC ISO 8601** (`2026-09-21T09:00:00Z`). Convert to local time for
   display only. Date-only fields such as `work_date` are plain `YYYY-MM-DD` strings;
   don't parse them as datetimes, or they shift by a day in some timezones.

---

## 2. Generate TypeScript types from the schema

Don't hand-write API types. Generate them:

```bash
npx openapi-typescript https://<staging-host>:8443/api/schema/?format=json -o src/api/schema.d.ts
# The staging certificate is self-signed; if the download fails, save the file first:
curl -k "https://<staging-host>:8443/api/schema/?format=json" -o schema.json
npx openapi-typescript schema.json -o src/api/schema.d.ts
```

Pair it with `openapi-fetch` (or your preferred client) for typed requests. Regenerate
the types whenever the backend changes.

---

## 3. Authentication

JWT (JSON Web Tokens) with a short-lived **access token** and a rotating **refresh token**.

| Token | Lifetime | Used for |
|---|---|---|
| `access` | 15 minutes | `Authorization: Bearer <access>` on every API call |
| `refresh` | 7 days | Getting a new token pair |

### Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/v1/auth/token/` | `{email, password}` | `{access, refresh}` |
| POST | `/api/v1/auth/token/refresh/` | `{refresh}` | `{access, refresh}`, a **new** refresh token |
| POST | `/api/v1/auth/logout/` | `{refresh}` | 204 (refresh token revoked). No access token needed, so it works after expiry |
| GET | `/api/v1/auth/me/` | none | Current user, roles and permissions |
| POST | `/api/v1/auth/password/` | `{old_password, new_password}` | 204 (revokes all refresh tokens: log in again) |

Email login ignores upper/lower case. The login endpoint is rate-limited, so show the
`THROTTLED` error nicely.

### Refresh rotation: read this carefully

Each refresh returns a **new refresh token and permanently revokes the old one**.
Consequences:

- Always save the new `refresh` from the response. Reusing the old one fails with `401`.
- **Refresh only once at a time.** If two requests (or two browser tabs) refresh with
  the same token at the same moment, the second fails and the user is logged out. Wrap
  the refresh in a single shared promise so parallel 401s wait for one refresh.
- On `401` from a normal API call: refresh once, retry the request once. If the refresh
  itself fails, send the user to the login page.

**If users get logged out after ~15 minutes, the refresh isn't implemented.** A 401 must
trigger a refresh, not a logout. Minimal pattern (adapt to your fetch wrapper):

```ts
let refreshing: Promise<string> | null = null; // one refresh at a time, shared by all callers

async function refreshAccess(): Promise<string> {
  refreshing ??= (async () => {
    const res = await fetch(`${API}/api/v1/auth/token/refresh/`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh: tokens.refresh }),
    });
    if (!res.ok) throw new Error("refresh failed");
    const data = await res.json();
    tokens.access = data.access;
    tokens.refresh = data.refresh; // rotation: ALWAYS store the new refresh token
    return data.access;
  })().finally(() => { refreshing = null; });
  return refreshing;
}

export async function api(path: string, init: RequestInit = {}): Promise<Response> {
  const call = (access: string) =>
    fetch(`${API}${path}`, {
      ...init,
      headers: { ...init.headers, Authorization: `Bearer ${access}` },
    });
  let res = await call(tokens.access);
  if (res.status === 401) {
    try {
      res = await call(await refreshAccess()); // retry once with the new token
    } catch {
      logout(); // refresh token expired or revoked, so a real logout
    }
  }
  return res;
}
```

With the BFF setup (section 3), put the same logic in the Next.js server code that calls
Django, and update the `httpOnly` cookies with the new tokens. The session then lasts
as long as the user is active at least once every 7 days (the refresh lifetime).

### Where to keep tokens (recommended)

Use a **backend-for-frontend (BFF)** setup: Next.js route handlers or server actions call
Django on the server, and tokens live in `httpOnly`, `Secure`, `SameSite=Lax`
cookies set by Next.js. Benefits:

- Browser JavaScript never sees the tokens, so an XSS bug cannot steal them.
- The browser only talks to Next.js, so no CORS setup is needed.
- A single server-side refresh path makes the "refresh only once" rule easy to enforce.

If you instead call the API directly from the browser, keep the access token in
memory only (not `localStorage`). The backend must then list the frontend's origin in
`CORS_ALLOWED_ORIGINS`; ask the backend team to add it, because it is empty on staging.

### `GET /auth/me/`

```json
{
  "id": 7,
  "email": "manager@demo.local",
  "full_name": "Manager",
  "is_active": true,
  "roles": ["MANAGER"],
  "date_joined": "2026-09-30T10:40:00Z",
  "last_login": "2026-09-30T12:00:00Z",
  "permissions": ["attendance.correct", "attendance.view", "developer.view", "..."]
}
```

Load this once after login and cache it. Use `permissions` (not `roles`) to decide
what to show, because roles can be edited and a user can have several.

---

## 4. Permissions → what to show

Check permission codes, never role names:

```ts
const can = (code: string) => me.permissions.includes(code);
{can("rfid.block") && <BlockCardButton />}
```

| Permission | Unlocks |
|---|---|
| `user.view` / `user.manage` | User list / create, edit, deactivate users |
| `role.view` / `role.assign` | Role list / add or remove roles on users |
| `audit.view` | Audit log |
| `developer.view` / `.create` / `.update` / `.delete` | Developer pages |
| `rfid.view` | Cards, assignments, readers, scan history |
| `rfid.assign` | Register, assign, unassign, replace, retire cards |
| `rfid.block` | Block / unblock cards |
| `rfid.device.manage` | Register readers, rotate reader keys |
| `attendance.view` / `attendance.correct` | Attendance pages / add or void records |
| `seller.view` / `.create` / `.update` | Sellers and all service positions / create sellers / edit sellers and positions |
| `good.view` / `.create` / `.update` / `.delete` | All goods and stock history / create / edit and images / delete |
| `good.stock` | Restock, write off and adjust stock |
| `finance.view` | All developer accounts and transactions |
| `finance.deposit` | Deposit money to developer accounts |
| `finance.adjust` | Manual corrections; freeze, unfreeze, close, reopen accounts; reset PINs |
| `seller_finance.view` | All seller balances, ledgers and payouts |
| `seller_finance.payout` | Request payouts for any seller; approve, reject, mark processing, pay |
| `seller_finance.adjust` | Manual seller balance corrections |
| `purchase.view` | All purchases |
| `purchase.create` / `.confirm` / `.cancel` | Run any seller's till (normally sellers use their own; see below) |

Default roles, which admins can change:

| Role | Gets |
|---|---|
| BOSS | Everything |
| MANAGER | Developers, RFID, attendance, user list, audit log, seller/goods view |
| FINANCE_MANAGER | Developer and seller finance, developer view, purchase view, audit log |
| SELLER_MANAGER | Sellers, goods, purchase view, seller finance view |
| DEVELOPER | Nothing global: only their own data via `/me/` endpoints |
| SELLER | Nothing global; see *Seller self-service* below |

If `permissions` is empty, the user only has self-service pages (section 7).

### Seller self-service

A user linked to an **ACTIVE** seller manages that seller's own catalogue without any
global permission: service positions, goods, images, stock and stock history. The
same endpoints serve both cases, and the same goes for the **till** (purchases at their
own service positions) and **their own money** (balance, ledger, requesting and cancelling
payouts); the backend narrows lists to the seller's own
objects and returns `404` for other sellers' objects. To tell whether the user is a
seller, call `GET /sellers/me/` (`404 SELLER_PROFILE_NOT_FOUND` means no). A
SUSPENDED or CLOSED seller gets `403` on catalogue endpoints.

---

## 5. Request and response conventions

### Errors: always the same shape

```json
{
  "error": {
    "code": "CARD_ALREADY_ASSIGNED",
    "message": "This card is already assigned to a developer.",
    "details": {}
  }
}
```

- Switch on `code` (stable) and show `message`. Never parse `message`.
- Validation errors: `code` is `VALIDATION_ERROR`, and `details` maps each field to its
  messages. Show them next to the form fields:

  ```json
  {"error": {"code": "VALIDATION_ERROR", "message": "Invalid input.",
             "details": {"email": ["Another developer already uses this email."]}}}
  ```

| HTTP | Typical `code` | Frontend action |
|---|---|---|
| 400 | `VALIDATION_ERROR` | Show field errors from `details` |
| 401 | `NOT_AUTHENTICATED`, `TOKEN_NOT_VALID` | Refresh once, else go to login |
| 401 | `NO_ACTIVE_ACCOUNT` | Wrong email/password on login |
| 403 | `PERMISSION_DENIED`, `PRIVILEGE_ESCALATION` | "You don't have access" |
| 404 | `NOT_FOUND`, `DEVELOPER_PROFILE_NOT_FOUND` | Not-found state |
| 409 | Business rule codes (below) | Show `message`; usually refetch the item |
| 429 | `THROTTLED` | "Too many attempts, wait a minute" |
| 500 | `INTERNAL_ERROR` | Generic error; quote the `X-Request-ID` response header in bug reports |

Business-rule codes so far: `LAST_BOSS`, `ROLE_ALREADY_ASSIGNED`, `ROLE_NOT_ASSIGNED`,
`DEVELOPER_HAS_REPORTS`, `CARD_NOT_ACTIVE`, `CARD_ALREADY_ASSIGNED`,
`DEVELOPER_ALREADY_HAS_CARD`, `CARD_NOT_ASSIGNED`, `DEVELOPER_NOT_ASSIGNABLE`,
`INVALID_CARD_TRANSITION`, `RECORD_ALREADY_VOID`, `POSITION_HAS_GOODS`, `INSUFFICIENT_STOCK`
(`details: {available, requested}`), `STOCK_NOT_TRACKED`, `NO_STOCK_CHANGE`,
`TOO_MANY_IMAGES`, `SELLER_PROFILE_NOT_FOUND`, `INSUFFICIENT_BALANCE`
(`details: {balance, required}`), `ACCOUNT_NOT_ACTIVE`, `DEPOSIT_LIMIT_EXCEEDED`
(`details: {max}`), `SELF_TRANSACTION_FORBIDDEN`, `IDEMPOTENCY_KEY_REUSED`,
`INVALID_ACCOUNT_TRANSITION`, `PURCHASE_NOT_DRAFT`, `PURCHASE_EMPTY`, `GOOD_NOT_AVAILABLE`,
`SELLER_NOT_ACTIVE`, `CARD_NOT_USABLE`, `DEVELOPER_NOT_ACTIVE`, `SELF_PURCHASE_FORBIDDEN`,
`PIN_NOT_SET`, `INVALID_PIN` (`details: {attempts_remaining}`), `PIN_LOCKED` (HTTP 423,
`details: {locked_until}`), `INSUFFICIENT_SELLER_BALANCE` (`details: {available}`),
`INVALID_PAYOUT_TRANSITION`, `SELF_APPROVAL_FORBIDDEN`, `OWN_SELLER_FORBIDDEN`, `CONFLICT`.

### Lists: pagination, search, filters, sorting

Every list endpoint returns:

```json
{"count": 123, "next": "https://.../?page=3", "previous": "https://.../?page=1", "results": [ ... ]}
```

| Query param | Meaning |
|---|---|
| `page`, `page_size` | Page number (from 1); page size default 50, max 200 |
| `search` | Free-text search (fields listed per endpoint below) |
| `ordering` | Sort field; prefix `-` for descending: `?ordering=-created_at` |
| any filter | Exact filters, e.g. `?status=ACTIVE&department=Engineering` |

Keep list state in the URL (`?page=2&search=ada&status=ACTIVE`) so views can be
bookmarked and survive a reload. Debounce search input (~300 ms).

`/api/v1/roles/` is the only list that is **not** paginated: it returns a plain array.

### Writes

- Send JSON with `Content-Type: application/json`.
- Updates use **PATCH** with only the changed fields. There is no PUT.
- Actions that change state are `POST .../{id}/<action>/` (e.g. `/rfid/cards/5/block/`).
  They return the updated object, so replace your cached copy with it.
- Nothing important is hard-deleted. `DELETE /developers/{id}/` is a soft delete;
  users are deactivated with `PATCH {"is_active": false}`; cards are retired.

### Money-moving requests need an `Idempotency-Key`

Deposits, adjustments, purchase confirmation and payout requests require an
`Idempotency-Key` header:

```ts
// Create the key once per user action, e.g. when the confirm dialog opens,
// and reuse the same key if you retry that action.
const idempotencyKey = crypto.randomUUID();
await api.post("/finance/deposits/", body, { headers: { "Idempotency-Key": idempotencyKey } });
```

- Same key, same request again (retry after a timeout, double-click): the backend returns
  the **original** result with `200` instead of `201`, and no money moves twice.
- Same key, different request: `409 IDEMPOTENCY_KEY_REUSED`.
- **Never** generate a new key for an automatic retry; only for a new user action.

### Request IDs

Every response has an `X-Request-ID` header. Show it in error dialogs, or log it: the
backend team can use it to find the exact request in the server logs.

---

## 6. Endpoint reference (current modules)

`{id}` is a numeric id unless noted. Full request/response shapes are in Swagger.

### Users and roles

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/users/` | `user.view` | Filters: `is_active`, `role`. Search: email, name. Ordering: `email`, `full_name`, `date_joined`, `last_login` |
| POST | `/users/` | `user.manage` | `{email, full_name?, password}`; password rules enforced |
| GET/PATCH | `/users/{id}/` | `user.view` / `user.manage` | PATCH: `full_name`, `is_active` |
| POST | `/users/{id}/roles/` | `role.assign` | `{role: "MANAGER"}` |
| DELETE | `/users/{id}/roles/{role_code}/` | `role.assign` | |
| GET | `/roles/`, `/roles/{code}/` | `role.view` | Each role includes its permission codes |

Users can't grant roles with more permissions than they have themselves
(`PRIVILEGE_ESCALATION`), and the last BOSS can't be removed or deactivated (`LAST_BOSS`).

### Developers

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/developers/` | `developer.view` | Filters: `status`, `department`, `manager`, `has_user`, `started_after`, `started_before`. Search: name, email, employee number, department, title |
| POST | `/developers/` | `developer.create` | |
| GET/PATCH/DELETE | `/developers/{id}/` | view / update / delete | DELETE = soft delete, only for mistakes; use `status: "TERMINATED"` for leavers |
| GET | `/developers/me/` | logged in | Own profile |

`status`: `ACTIVE`, `ON_LEAVE`, `SUSPENDED`, `TERMINATED`. Responses include
`manager_detail` (`{id, employee_number, full_name, department}`) for display, while
`manager` is the id you send.

### RFID

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/rfid/cards/` | `rfid.view` | Filters: `status`, `assigned` (true/false), `developer`. Search: UID, label, holder name |
| POST | `/rfid/cards/` | `rfid.assign` | `{uid, label?, notes?}`; UID may contain `:`, `-` or spaces |
| PATCH | `/rfid/cards/{id}/` | `rfid.assign` | `label`, `notes` only |
| POST | `/rfid/cards/{id}/assign/` | `rfid.assign` | `{developer}` |
| POST | `/rfid/cards/{id}/unassign/` | `rfid.assign` | |
| POST | `/rfid/cards/{id}/replace/` | `rfid.assign` | `{new_card_uid, new_card_label?, reason?}`; returns the **new** card |
| POST | `/rfid/cards/{id}/block/`, `/unblock/` | `rfid.block` | `{reason?}` |
| POST | `/rfid/cards/{id}/retire/` | `rfid.assign` | Card must be unassigned |
| GET | `/rfid/assignments/` | `rfid.view` | Card ownership history. Filters: `card`, `developer`, `active`, `end_reason` |
| GET/POST/PATCH | `/rfid/devices/` | view / `rfid.device.manage` | Readers. Filters: `is_active`, `purpose` |
| POST | `/rfid/devices/{id}/rotate-key/` | `rfid.device.manage` | Returns a new `api_key` |
| GET | `/rfid/events/` | `rfid.view` | Raw scan log. Filters: `device`, `card`, `developer`, `result`, `uid`, `event_after`, `event_before` |

- Card `status`: `ACTIVE`, `BLOCKED` (keeps its owner, scans rejected), `RETIRED` (permanent).
  `current_assignment` is `null` when the card is unassigned.
- **Reader API keys are shown only once** (in the create and rotate-key responses). Show
  them in a dialog with a copy button and a clear "you won't see this again" warning.
- Scan `result`: `ACCEPTED`, `DUPLICATE`, `UNKNOWN_CARD`, `UNASSIGNED_CARD`, `BLOCKED_CARD`,
  `RETIRED_CARD`, `INACTIVE_DEVELOPER`.
- **Registering a new card from a tap:** filter `/rfid/events/?result=UNKNOWN_CARD`
  for the latest unknown UID, then prefill the "register card" form with it.
- `POST /rfid/events/` is for reader hardware only (it uses a device key, not a user
  login). The frontend never calls it.

### Attendance

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/attendance/daily/` | `attendance.view` | One row per developer per day. Filters: `developer`, `work_date`, `date_from`, `date_to`, `status`, `department` |
| GET | `/attendance/records/` | `attendance.view` | Individual moments. Filters: `developer`, `work_date`, `date_from`, `date_to`, `source`, `event_type`, `is_void`, `device` |
| POST | `/attendance/records/` | `attendance.correct` | Manual record: `{developer, event_time, note}` (note required) |
| POST | `/attendance/records/{id}/void/` | `attendance.correct` | `{reason}` (required) |
| GET | `/attendance/daily/me/`, `/attendance/records/me/` | logged in | Own attendance; same filters |

- Daily `status`: `PRESENT` or `INCOMPLETE` (a single scan, or an IN without an OUT).
  **No row means no scans that day**; absence and lateness aren't calculated yet.
- `worked_hours` is a ready-to-display number; `worked_seconds` is exact.
- `event_type` is `SCAN` until the company picks an IN/OUT rule, then `IN`/`OUT`. It can
  change after the rule changes, so always display the value from the API; never derive it.
- Show voided records struck through, with `void_reason`, rather than hiding them.

### Sellers and service positions

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/sellers/` | `seller.view` | Filters: `status`, `has_user`. Search: name, contact, email |
| POST | `/sellers/` | `seller.create` | `{name, user?, contact_name?, email?, phone?, notes?}`; `user` = the seller's login |
| GET/PATCH | `/sellers/{id}/` | `seller.view` / `seller.update` | No DELETE: close with `status: "CLOSED"` |
| GET | `/sellers/me/` | logged in | Own seller profile (any status) |
| GET/POST | `/service-positions/` | `seller.view` / `seller.update`, or own seller | Filters: `seller`, `is_active`. Sellers omit `seller` on create; managers must send it |
| GET/PATCH/DELETE | `/service-positions/{id}/` | same | DELETE is soft and fails with `POSITION_HAS_GOODS` while it has goods |

Seller `status`: `ACTIVE`, `SUSPENDED`, `CLOSED`.

### Goods and stock

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/goods/` | `good.view`, or own seller | Filters: `seller`, `service_position`, `is_active`, `track_stock`, `in_stock`, `price_min`, `price_max`. Search: name, description, SKU, seller. Ordering: `name`, `price`, `quantity`, `created_at` |
| POST | `/goods/` | `good.create`, or own seller | `{service_position, name, price, description?, sku?, is_active?, track_stock?, initial_quantity?}` |
| GET/PATCH/DELETE | `/goods/{id}/` | view / `good.update` / `good.delete`, or own seller | DELETE is soft. `quantity` can't be PATCHed |
| POST | `/goods/{id}/stock/` | `good.stock`, or own seller | See below. Returns the stock movement |
| POST | `/goods/{id}/images/` | `good.update`, or own seller | `multipart/form-data`: `image`, `alt_text?`, `position?`. Returns the whole good |
| DELETE | `/goods/{id}/images/{image_id}/` | same | |
| GET | `/inventory/movements/` | `good.view`, or own seller | Stock history. Filters: `good`, `seller`, `kind`, `created_after`, `created_before` |

- **Money is a string**, e.g. `"price": "3.20"`, never a float. Send prices as strings
  too. Display it with the `currency` field (ISO code, e.g. `"USD"`) via `Intl.NumberFormat`,
  and never do price arithmetic with JavaScript floats.
- **Stock changes** (`POST /goods/{id}/stock/`):
  - `{"kind": "RESTOCK", "quantity": 20}`: add units
  - `{"kind": "DAMAGE", "quantity": 2, "reason": "Dropped"}`: write off units (reason required)
  - `{"kind": "ADJUSTMENT", "counted_quantity": 17, "reason": "Monthly count"}`: set stock to a
    physical count (reason required)
- `SALE` and `RETURN` movements are created by purchases later, never by the frontend.
- `track_stock: false` goods (made-to-order food, services) have no quantity; stock
  changes on them fail with `STOCK_NOT_TRACKED`. Treat them as always available.
- Images: JPEG, PNG or WebP, max 5 MB, max 10 per good. `image` in responses is a full
  URL. Lowest `position` is the main image.

### Developer accounts and money

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/finance/accounts/` | `finance.view` | Filters: `status`, `developer`, `department`, `balance_min`, `balance_max`. Search: developer name, number, email. Ordering: `balance`, `developer__full_name`, `updated_at` |
| GET | `/finance/accounts/{id}/` | `finance.view` | |
| GET | `/finance/accounts/me/` | logged in | Own balance (needs a developer profile) |
| POST | `/finance/accounts/{id}/freeze/`, `/unfreeze/`, `/close/`, `/reopen/` | `finance.adjust` | `{reason?}`. Close only with a zero balance |
| GET | `/finance/transactions/` | `finance.view` | Ledger. Filters: `account`, `developer`, `kind`, `reference`, `created_after`, `created_before` |
| GET | `/finance/transactions/me/` | logged in | Own ledger |
| POST | `/finance/deposits/` | `finance.deposit` | `{developer, amount, description?}` + `Idempotency-Key` header |
| POST | `/finance/adjustments/` | `finance.adjust` | `{developer, amount, reason}`; amount is signed (`"-5.00"` debits) + `Idempotency-Key` |

- Every developer has exactly one account, created automatically.
- Account `status`: `ACTIVE`; `FROZEN` (can receive money, cannot spend); `CLOSED`.
- Transaction `kind`: `DEPOSIT` (+), `PURCHASE` (−), `REFUND` (+), `ADJUSTMENT` (±). `amount`
  is signed, and `balance_after` is the balance right after that entry, which is handy
  for a statement view.
- **The ledger is never edited.** A mistaken deposit is corrected with a negative
  adjustment, and both entries stay visible.
- Deposits and adjustments return the new transaction (`balance_after` = new balance).
- A single deposit is capped (`DEPOSIT_LIMIT_EXCEEDED` includes the `max`).
- Users can't deposit to or adjust **their own** account (`SELF_TRANSACTION_FORBIDDEN`),
  so hide that action when the target developer is the logged-in user.

### Purchases (the till)

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/purchases/` | own seller, or `purchase.create` | `{service_position}`: opens a DRAFT bucket |
| POST | `/purchases/{id}/items/` | same | `{good, quantity?}`; adding a good already in the bucket increases its quantity |
| PATCH / DELETE | `/purchases/{id}/items/{item_id}/` | same | PATCH `{quantity}` / DELETE removes the line |
| POST | `/purchases/{id}/confirm/` | own seller, or `purchase.confirm` | `{card_uid, pin}` + `Idempotency-Key`: charges the card holder |
| POST | `/purchases/{id}/cancel/` | own seller, or `purchase.cancel` | Drafts only |
| GET | `/purchases/`, `/purchases/{id}/` | own seller, or `purchase.view` | Filters: `status`, `seller`, `service_position`, `developer`, `confirmed_after`, `confirmed_before`, `total_min`, `total_max` |
| GET | `/purchases/me/` | logged in | The developer's own purchases |
| POST | `/finance/accounts/me/pin/` | logged in | Developer sets `{pin}` or changes it with `{pin, current_pin}` |
| POST | `/finance/accounts/{id}/reset-pin/` | `finance.adjust` | Clears a forgotten PIN and any lockout |

Every item/confirm/cancel call returns the **whole purchase**, so re-render the bucket from
the response.

**Till flow:**
1. Seller picks their service position → `POST /purchases/` (keep the returned `id`).
2. Seller adds goods → `POST /purchases/{id}/items/`. `total` and `unit_price` show current prices.
3. The developer taps their card: read the UID from the till's card reader (usually a
   USB reader that types the UID like a keyboard), and the developer types their PIN.
   Mask the PIN field, never store or log it, and clear it after each attempt.
4. `POST /purchases/{id}/confirm/` with a **new** `Idempotency-Key` generated when the
   confirm step starts; reuse it only for automatic retries of that same attempt.
5. On `201`: show success with `developer.full_name`, `total` and `balance_after`
   (the developer's remaining balance), then start a new purchase.

**Confirmation is all-or-nothing.** Stock, the developer's balance and the purchase change
together, or nothing changes. On any error the purchase stays a DRAFT and can be retried:

| `code` | Show |
|---|---|
| `INVALID_PIN` | "Wrong PIN, N attempts left" (`details.attempts_remaining`) |
| `PIN_LOCKED` (423) | "PIN locked until …" (`details.locked_until`); another payment is needed |
| `PIN_NOT_SET` | "Set your PIN first" (developer: My account → PIN) |
| `INSUFFICIENT_BALANCE` | Balance and amount needed (`details.balance`, `details.required`) |
| `INSUFFICIENT_STOCK` | Which good ran out (`details.good`, `available`) |
| `CARD_NOT_USABLE`, `DEVELOPER_NOT_ACTIVE`, `ACCOUNT_NOT_ACTIVE` | The `message` |

- Prices are fixed at confirmation: a confirmed purchase shows what was actually charged.
- **Confirmed purchases are final**; there are no refunds. Only drafts can be cancelled.
- Sellers can't charge their own card (`SELF_PURCHASE_FORBIDDEN`).
- `SELLER_NOT_ACTIVE`: the seller or service position was suspended or deactivated.

### Seller finance and payouts

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/seller-finance/accounts/` | `seller_finance.view` | Every seller's `balance`, `reserved`, `available_balance` |
| GET | `/seller-finance/accounts/me/` | logged in | The seller's own account |
| GET | `/seller-finance/transactions/` | `seller_finance.view`, or own seller | Ledger. Filters: `seller`, `kind`, `reference`, `created_after`, `created_before` |
| GET | `/seller-finance/payouts/` | `seller_finance.view`, or own seller | Filters: `seller`, `status`, `created_after`, `created_before` |
| POST | `/seller-finance/payouts/` | own seller, or `seller_finance.payout` | `{amount, note?}` (+ `seller` for finance staff) + `Idempotency-Key` |
| POST | `/seller-finance/payouts/{id}/approve/` | `seller_finance.payout` | Not by the requester |
| POST | `/seller-finance/payouts/{id}/processing/` | `seller_finance.payout` | Optional step |
| POST | `/seller-finance/payouts/{id}/pay/` | `seller_finance.payout` | `{payment_reference}` (transfer or receipt no.) |
| POST | `/seller-finance/payouts/{id}/reject/` | `seller_finance.payout` | `{reason}` |
| POST | `/seller-finance/payouts/{id}/cancel/` | own seller, or `seller_finance.payout` | Only while REQUESTED |
| POST | `/seller-finance/adjustments/` | `seller_finance.adjust` | `{seller, amount, reason}`, signed + `Idempotency-Key` |

- Every confirmed purchase credits its seller with the full amount (no commission), in
  the same transaction that charges the developer. Ledger `kind`: `SALE` (+),
  `PAYOUT` (−), `ADJUSTMENT` (±); `reference` is `purchase:<id>` or `payout:<id>`.
- Payout `status`: `REQUESTED` → `APPROVED` → (`PROCESSING` →) `PAID`, or `REJECTED` /
  `CANCELLED`. Show the allowed buttons per status:

  | Status | Seller | Finance |
  |---|---|---|
  | REQUESTED | Cancel | Approve (not own request), Reject |
  | APPROVED | none | Processing, Pay, Reject |
  | PROCESSING | none | Pay, Reject |
  | PAID / REJECTED / CANCELLED | none | none |

- **Open payouts reserve money:** `available_balance` = `balance` − `reserved`. A request
  above `available_balance` fails with `INSUFFICIENT_SELLER_BALANCE`. The balance itself
  drops only when a payout is PAID.
- Hide Approve on payouts the logged-in user requested (`requested_by` = `me.id`).

### Audit log

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/audit-logs/` | `audit.view` | Filters: `action`, `entity_type`, `entity_id`, `actor`, `created_after`, `created_before` |

Each entry has `old_values` / `new_values` (only the changed fields): render them as a
before → after table.

---

## 7. Suggested pages

| Page | Main endpoints | Visible with |
|---|---|---|
| Login, change password | `auth/*` | everyone |
| My profile, my attendance | `developers/me/`, `attendance/*/me/` | anyone with a developer profile |
| Developers (list, detail, edit) | `developers/`, `rfid/cards/?developer=`, `attendance/daily/?developer=` | `developer.view` |
| Cards (list, detail with history and actions) | `rfid/cards/`, `rfid/assignments/?card=` | `rfid.view` |
| Readers | `rfid/devices/` | `rfid.view` |
| Live scan monitor | poll `rfid/events/?ordering=-event_time&page_size=20` every few seconds | `rfid.view` |
| Attendance (daily table by date/department, corrections) | `attendance/daily/`, `attendance/records/` | `attendance.view` |
| Users and roles | `users/`, `roles/` | `user.view` |
| Sellers (list, detail with positions) | `sellers/`, `service-positions/?seller=` | `seller.view` |
| Goods catalogue (list, edit, images, stock dialog, stock history) | `goods/`, `inventory/movements/?good=` | `good.view` |
| My shop (seller self-service: positions, goods, stock) | `sellers/me/`, `service-positions/`, `goods/`, `inventory/movements/` | active seller |
| Developer accounts (balances, statement, deposit dialog, corrections) | `finance/accounts/`, `finance/transactions/?developer=`, `finance/deposits/` | `finance.view` |
| My balance, statement and PIN | `finance/accounts/me/`, `finance/transactions/me/`, `finance/accounts/me/pin/` | anyone with a developer profile |
| **Till** (choose position, build bucket, card + PIN confirm) | `purchases/`, `goods/?service_position=&is_active=true&in_stock=true` | active seller |
| Sales history | `purchases/?status=CONFIRMED` | active seller, or `purchase.view` |
| My earnings and payouts (seller) | `seller-finance/accounts/me/`, `seller-finance/transactions/`, `seller-finance/payouts/` | active seller |
| Seller balances and payout queue (finance) | `seller-finance/accounts/`, `seller-finance/payouts/?status=REQUESTED` | `seller_finance.view` |
| My purchases | `purchases/me/` | anyone with a developer profile |
| Audit log | `audit-logs/` | `audit.view` |

Build the navigation from `me.permissions` so each user only sees their pages.

---

## 8. Local development

1. Point the frontend at staging: `NEXT_PUBLIC_API_URL` (or a server-only `API_URL` for the
   BFF setup) = `https://<staging-host>:8443`.
2. The staging certificate is self-signed. For server-side calls from Node during
   development, trust it by setting `NODE_EXTRA_CA_CERTS=/path/to/staging.crt` (ask the
   backend team for the file). Don't use `NODE_TLS_REJECT_UNAUTHORIZED=0`.
3. Demo accounts exist for each role (`boss@demo.local`, `manager@demo.local`,
   `finance_manager@demo.local`, `seller_manager@demo.local`, `developer@demo.local`,
   `seller@demo.local`). Ask the backend team for passwords. `developer@demo.local` is
   linked to a developer profile, so the `/me/` pages have data.

---

## 9. Production deployment (offline server)

The frontend will run on the same offline server as the backend:

- Build with `output: "standalone"` in `next.config.js`, so the build carries its own
  dependencies and needs no `npm install` on the server.
- Build on a machine with internet, then copy `.next/standalone`, `.next/static` and
  `public/` to the server along with a matching Node.js runtime. The server has no
  internet, so it can't download Node.
- nginx serves one hostname: `/api/`, `/admin/`, `/static/`, `/health/` go to Django, and
  everything else goes to Next.js. Same origin means no CORS.
- Coordinate with the backend team to add the Next.js service to the offline install
  bundle.

---

## 10. Not built yet

These APIs are still to come; don't build screens against guesses. Permission codes for
them already exist in `me.permissions`.

- Generic approvals (e.g. for large deposits or adjustments)

Ask the backend team for the current state before starting on any of these.
