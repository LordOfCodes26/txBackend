# Frontend Build Guide

How to build the Next.js frontend against this backend's REST API. The backend is
Django + Django REST Framework; the frontend only talks to it over HTTP.

- **API base:** `/api/v1/`
- **Interactive docs (Swagger):** `/api/docs/` (try every endpoint in the browser)
- **OpenAPI schema:** `/api/schema/` (YAML; add `?format=json` for JSON)
- **Staging server:** `https://<staging-host>:8443` (self-signed certificate) or `http://<staging-host>:8088`; ask the backend team for the address

The OpenAPI schema is the source of truth. If this guide and the schema ever disagree,
the schema wins, and please report the mismatch. How the RFID hardware talks to the
server (doors, till readers) is in `docs/DEVICE_INTEGRATION.md`; the web app only manages
devices and shows their results.

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
   display only. (The server's company timezone is still UTC; it decides where attendance
   days and rental opening hours start.) Date-only fields such as `work_date` are plain `YYYY-MM-DD` strings;
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
| `stats.view` | Company statistics (`GET /stats/`, the BOSS dashboard) |
| `developer.view` / `.create` / `.update` / `.delete` | Developer pages |
| `rfid.view` | Cards, assignments, devices, buildings, scan history |
| `rfid.assign` | Register, assign, unassign, replace, retire cards |
| `rfid.block` | Block / unblock cards |
| `rfid.device.manage` | Register devices (doors and till readers), rotate keys, set door IPs, manage buildings |
| `attendance.view` / `attendance.correct` | Occupancy and attendance pages / add or void records |
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
| ADMIN | Everything: users, roles, settings and all data |
| BOSS | **Read-only**: every `*.view` permission (all lists and details) plus `stats.view` (the statistics dashboard). Can't create, change or delete anything |
| MANAGER | Developers, RFID, attendance, user list, audit log, seller/goods view |
| FINANCE_MANAGER | Developer and seller finance, developer view, purchase view, audit log |
| DEVELOPER | Nothing global: only their own data via `/me/` endpoints |
| SELLER | Nothing global; see *Seller self-service* below |
| BUILDING_OWNER | Like BOSS (read-only + statistics) but **only their buildings**, including the stores there; no users, roles or audit log (see *Building owners* below) |
| BUILDING_MANAGER | Developer view/create/update, RFID view/assign/block/devices, attendance view/correct, purchase view, **all limited to their buildings** (see *Building managers* below) |

If `permissions` is empty, the user only has self-service pages (section 7).

### Assigning a seller login to a store

A store (seller) is run by **one login with the SELLER role**: set it with
`PATCH /sellers/{id}/ {"user": <user id>}` (ADMIN or `seller.update`; `null` unlinks).
Find candidates with `GET /users/?role=SELLER`. The store then shows `user_email`.

- Only active users **with the SELLER role** can be linked (`VALIDATION_ERROR` on `user`:
  "This user doesn't have the SELLER role."); the same holds for a position's `manager`.
- A login runs one store, either as its owner or as a position manager, not both.
- Store access needs **both** the SELLER role and the link: removing either removes access.
- The SELLER role itself must have **no permissions** (Accounts → Roles in the admin).
  Any permission there applies to every store, e.g. `good.view` shows all stores' goods.
  Show the seller menus when `GET /sellers/me/` returns a seller, not from `permissions`.

### Seller self-service

A user linked to an **ACTIVE** seller manages that seller's own catalogue without any
global permission: service positions, goods, images, stock and stock history. The
same endpoints serve both cases, and the same goes for the **till** (purchases at their
own service positions) and **their own money** (balance, ledger, requesting and cancelling
payouts); the backend narrows lists to the seller's own
objects and returns `404` for other sellers' objects. To tell whether the user is a
seller, call `GET /sellers/me/` (`404 SELLER_PROFILE_NOT_FOUND` means no). A
SUSPENDED or CLOSED seller gets `403` on catalogue endpoints.

### Building managers

A user with the **BUILDING_MANAGER** role who is listed in a building's `managers`
(`PATCH /rfid/buildings/{id}/ {"managers": [<user ids>]}`, by an admin) sees and manages
only that building's data, through the normal endpoints:

| Area | What they get |
|---|---|
| Developers | Only developers whose `building` (home building) is theirs; they must set their building when creating, can't move a developer to another building, can't delete |
| Cards | Their developers' cards plus unassigned cards; assign only to their developers |
| Door devices | Only their building's doors; create doors only in their building (no till readers) |
| Buildings | Only their own; may rename, can't create, delete or change `managers` |
| Scan history | Scans at their building's doors |
| Attendance | Records and daily summaries of their developers; manual records / voids only for them |
| Occupancy | `/attendance/occupancy/` and `/people/` show only their building; the `ws/occupancy/` stream too |
| Sales | Purchases and bookings of sell positions in their building, and `GET /purchases/performance/`; read-only |

Other objects return `404`; changes that would leave their building fail with a
validation error. If the same user also has a role that grants a permission everywhere
(e.g. MANAGER for developers), that permission is not limited. A building manager listed
in no building sees nothing.

**Sales performance** (`GET /purchases/performance/`, `purchase.view`, also for sellers and
position managers for their own): one row per sell position with
`{service_position, service_position_name, seller, seller_name, building, building_name,
sales_count, sales_total, bookings_count, bookings_total, total}`, highest total first.
Use the purchase list filters, e.g. `?confirmed_after=2026-10-01T00:00:00Z&confirmed_before=…`.

### Building owners

A user with the **BUILDING_OWNER** role who is listed in a building's `owners` sees, **read-only**,
everything a BOSS sees, limited to their buildings:

| Area | What they see |
|---|---|
| People | Developers whose home `building` is theirs; their cards, attendance, occupancy, developer accounts and ledger |
| Doors | Their buildings' door devices and scans |
| Stores | Sellers with a position in their buildings; those positions, their goods and stock history; purchases, bookings and `/purchases/performance/` of those positions |
| Seller money | Balance, ledger and payouts only of stores whose positions are **all** in their buildings (a store that also sells elsewhere shows its sales here, not its seller-wide money) |
| Statistics | `GET /stats/` with every figure limited to their buildings (`buildings` in the response) |

Users, roles and the audit log are company-wide, so building owners don't get them (`403`).
Every change returns `403`. If the user also has an unrestricted role such as BOSS, they
see everything.

### Assigning buildings to owners and managers

1. Give the user the role (`POST /users/{id}/roles/ {"role": "BUILDING_OWNER"}` or
   `BUILDING_MANAGER`).
2. Add them to the building: `PATCH /rfid/buildings/{id}/` (ADMIN, `rfid.device.manage`)
   with the **whole** list, e.g. `{"owners": [7, 12]}` or `{"managers": [9]}`. The list
   replaces the previous one: to add someone, send the current ids plus theirs; `[]`
   removes everyone. `GET /rfid/buildings/` returns `owners` and `managers` (user ids).
3. One user can own or manage several buildings (add them to each).

Users without the matching role are rejected: `VALIDATION_ERROR` on `owners` /
`managers`, "These users don't have the BUILDING_OWNER role: a@x.com". Candidates:
`GET /users/?role=BUILDING_OWNER` (or `BUILDING_MANAGER`). In the Django admin the same is
possible under **RFID → Buildings** (two-box pickers for owners and managers).

Suggested UI on the building page: two multi-selects, "Owners" (users with
BUILDING_OWNER) and "Managers" (users with BUILDING_MANAGER), saved with one PATCH.

### Position managers

Each service position can have **one manager login** (`manager` on the position). A
position manager is narrowed further than the seller's owner (`Seller.user`):

| | Seller's owner | Position manager |
|---|---|---|
| Goods, images, stock, stock history | all the seller's positions | **only their position(s)** |
| Till sales, booking checkouts, bookings, counter WebSocket | all positions | **only their position(s)** |
| Service positions | list, create, edit, delete, assign managers / building | list and edit **their own** (name, location); can't change `manager`, `building`, `is_active`, can't create or delete |
| Seller money (balance, ledger, payouts) | yes | **no** (`403`) |

`GET /sellers/me/` returns the seller for both. To know which positions a manager has,
list `GET /service-positions/` (it's already narrowed). Other positions' objects return
`404`; creating goods or sales at another position fails with a validation error on
`service_position`. Staff with `seller.update` (or the owner) assign the manager:
`PATCH /service-positions/{id}/ {"manager": <user id>}` (`null` removes it). A seller's
owner can't also be a position manager.

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
             "details": {"employee_number": ["This employee number is already in use."]}}}
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

Business-rule codes so far: `LAST_ADMIN`, `ROLE_ALREADY_ASSIGNED`, `ROLE_NOT_ASSIGNED`,
`DEVELOPER_HAS_REPORTS`, `CARD_NOT_ACTIVE`, `CARD_ALREADY_ASSIGNED`,
`DEVELOPER_ALREADY_HAS_CARD`, `CARD_NOT_ASSIGNED`, `DEVELOPER_NOT_ASSIGNABLE`,
`INVALID_CARD_TRANSITION`, `RECORD_ALREADY_VOID`, `POSITION_HAS_GOODS`, `INSUFFICIENT_STOCK`
(`details: {available, requested}`), `STOCK_NOT_TRACKED`, `NO_STOCK_CHANGE`,
`TOO_MANY_IMAGES`, `SELLER_PROFILE_NOT_FOUND`, `INSUFFICIENT_BALANCE`
(`details: {balance, required}`), `ACCOUNT_NOT_ACTIVE`, `DEPOSIT_LIMIT_EXCEEDED`
(`details: {max}`), `SELF_TRANSACTION_FORBIDDEN`, `IDEMPOTENCY_KEY_REUSED`,
`INVALID_ACCOUNT_TRANSITION`, `PURCHASE_NOT_DRAFT`, `PURCHASE_EMPTY`, `GOOD_NOT_AVAILABLE`,
`SELLER_NOT_ACTIVE`, `CARD_NOT_USABLE`, `DEVELOPER_NOT_ACTIVE`, `SELF_PURCHASE_FORBIDDEN`,
`PIN_NOT_SET`, `CARD_NOT_PRESENTED`, `MANUAL_CARD_ENTRY_DISABLED`, `INVALID_PIN` (`details: {attempts_remaining}`), `PIN_LOCKED` (HTTP 423,
`details: {locked_until}`), `INSUFFICIENT_SELLER_BALANCE` (`details: {available}`),
`INVALID_PAYOUT_TRANSITION`, `SELF_APPROVAL_FORBIDDEN`, `OWN_SELLER_FORBIDDEN`,
`RENTAL_NOT_AVAILABLE`, `INVALID_SLOT` (with `details` explaining the rule), `SLOT_UNAVAILABLE`,
`DAILY_LIMIT_REACHED` (`details: {max_slots_per_day, already_booked, remaining}`),
`ALREADY_BOOKED_THEN` (`details: {booking, good, start, end}`),
`IN_USE` (deleting something still referenced, e.g. a building that has doors), `CONFLICT`.

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

Deposits, adjustments, purchase confirmation (also for bookings) and payout requests require an
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

### Language (English / Korean)

Every API message (`error.message`, validation `details`, the till's `display_message`) is
available in **English** and **Korean in DPRK usage (조선어)**. Send the user's language on
every request, from the Next.js server actions too:

```ts
headers: { "Accept-Language": locale }   // "ko" or "en"
```

- `ko`, `ko-KP` and `ko-KR` all get the DPRK texts. No header (or another language) → English.
- The response carries `Content-Language: ko-kp` or `en`.
- **Only texts change.** `error.code`, enum values (`ACTIVE`, `PRODUCT`, `IN`…), field names
  and `details` keys stay English. Keep switching on `code`, and translate enum values to
  labels in the frontend.
- Data people typed (names, goods, descriptions) is returned as stored, not translated.
- Reader texts on the reader itself use the server's `DEVICE_LANGUAGE`, not the header.

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
(`PRIVILEGE_ESCALATION`), and the last ADMIN can't be removed or deactivated (`LAST_ADMIN`).

### Developers

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/developers/` | `developer.view` | Filters: `status`, `department`, `building`, `manager`, `has_user`, `started_after`, `started_before`, `out_after`, `out_before`, `birthday_month` (1–12). Search: name, employee number, department, title, phone. Ordering: `full_name`, `employee_number`, `department`, `start_date`, `out_date`, `birthday`, `created_at` |
| POST | `/developers/` | `developer.create` | |
| GET/PATCH/DELETE | `/developers/{id}/` | view / update / delete | DELETE = soft delete, only for mistakes; use `status: "TERMINATED"` for leavers |
| GET | `/developers/me/` | logged in | Own profile |

Fields: `employee_number`, `full_name`, `phone`, `home_address`, `birthday`, `department`,
`position_title`, `building` (optional home building; read-only `building_name`), `manager`,
`start_date`, `out_date` (last working day), `status`, `user`.
Developers have **no email field**; a developer's login email lives on their user account.
Dates are `YYYY-MM-DD`. `out_date` can't be before `start_date`, and `birthday` can't be in
the future. `home_address` and `birthday` are personal data: show them only on detail and
edit pages, not in list tables.

`status`: `ACTIVE`, `ON_LEAVE`, `SUSPENDED`, `TERMINATED`. Responses include
`manager_detail` (`{id, employee_number, full_name, department}`) for display, while
`manager` is the id you send.

### RFID

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/rfid/cards/` | `rfid.view` | Filters: `status`, `assigned` (true/false), `developer`. Search: UID, label, holder name |
| POST | `/rfid/cards/` | `rfid.assign` | `{uid, label?, notes?}`; UID may contain `:`, `-` or spaces |
| PATCH | `/rfid/cards/{id}/` | `rfid.assign` | `label`, `notes` only |
| POST | `/rfid/cards/{id}/assign/` | `rfid.assign` | `{developer, pin, pin_confirm}`: assigning a card **also sets the developer's purchase PIN**. Show two masked PIN fields the developer fills in themselves (4–6 digits, not trivial like 1111 / 1234). A bad or mismatched PIN assigns nothing (`VALIDATION_ERROR` on `pin` / `pin_confirm`). A new PIN replaces an old one |
| POST | `/rfid/cards/{id}/unassign/` | `rfid.assign` | |
| POST | `/rfid/cards/{id}/replace/` | `rfid.assign` | `{new_card_uid, new_card_label?, reason?}`; returns the **new** card |
| POST | `/rfid/cards/{id}/block/`, `/unblock/` | `rfid.block` | `{reason?}` |
| POST | `/rfid/cards/{id}/retire/` | `rfid.assign` | Card must be unassigned |
| GET | `/rfid/assignments/` | `rfid.view` | Card ownership history. Filters: `card`, `developer`, `active`, `end_reason` |
| GET | `/rfid/devices/` | `rfid.view` | Doors and till readers. Filters: `purpose`, `building`, `service_position`, `is_active`, `online` |
| POST / PATCH | `/rfid/devices/`, `/rfid/devices/{id}/` | `rfid.device.manage` | Register or edit a device (see *Devices* below). No DELETE: deactivate with `{"is_active": false}` |
| POST | `/rfid/devices/{id}/rotate-key/` | `rfid.device.manage` | Returns a new `api_key`; the old one stops working at once |
| GET | `/rfid/buildings/` | `rfid.view` | Buildings, e.g. `{"id": 1, "code": "B1", "name": "Building 1", "owners": [7], "managers": [9]}` (user ids). Plain array, not paginated. Building managers / owners see only their own |
| POST / PATCH / DELETE | `/rfid/buildings/`, `/rfid/buildings/{id}/` | `rfid.device.manage` | `{code, name, owners?, managers?}`. `owners` / `managers` replace the whole list and need the BUILDING_OWNER / BUILDING_MANAGER role (see *Assigning buildings to owners and managers*). A building with doors can't be deleted |
| GET | `/rfid/events/` | `rfid.view` | Raw scan log. Filters: `device`, `card`, `developer`, `result`, `uid`, `event_after`, `event_before` |

- Card `status`: `ACTIVE`, `BLOCKED` (keeps its owner, scans rejected), `RETIRED` (permanent).
  `current_assignment` is `null` when the card is unassigned.
- **Device API keys are shown only once** (in the create and rotate-key responses). Show
  them in a dialog with a copy button and a clear "you won't see this again" warning.
- Scan `result`: `ACCEPTED`, `DUPLICATE`, `UNKNOWN_CARD`, `UNASSIGNED_CARD`, `BLOCKED_CARD`,
  `RETIRED_CARD`, `INACTIVE_DEVELOPER`.
- **Registering a new card from a tap:** filter `/rfid/events/?result=UNKNOWN_CARD`
  for the latest unknown UID, then prefill the "register card" form with it.
- `POST /rfid/events/`, `/rfid/events/batch/` and `/rfid/device/heartbeat/` are for devices
  only (they use a device key, not a user login). The frontend never calls them. Device
  protocol: `docs/DEVICE_INTEGRATION.md`.
- Raw scans have `direction` (`IN` / `OUT` from doors, empty for till taps) and the
  `device_code`, so a live scan monitor can show "Door2 · in · Ada Lovelace".

**Devices.** There are two kinds; the registration form depends on `purpose`:

| `purpose` | Examples | Required / allowed fields | Authentication |
|---|---|---|---|
| `ATTENDANCE` | `Door1` (Building 1), `Door2` (Building 2) | `code`, `building`; optional `allowed_ip`, `name`, `location` | Fixed IP (`allowed_ip`) or API key |
| `TILL` | `Reader1`, `Reader2`, … | `code`, `sn` (the reader's serial number); no `building`, no `allowed_ip`. **Not tied to a counter or seller** | **Serial number + ID** (`sn`), or API key |

```json
POST /rfid/devices/  {"code": "Door1", "purpose": "ATTENDANCE", "building": 1, "allowed_ip": "10.20.0.11"}
POST /rfid/devices/  {"code": "Reader2", "purpose": "TILL", "sn": "ZK2024A0001234"}
```

- `code` must be exactly what the hardware sends as `ID` (`Door1`, `Reader2`, …).
- Validation errors to show next to the fields: a till with a `building` or `allowed_ip`, an
  attendance device with an `sn`, or a serial number already used by another device.
- `sn` is shown in full (upper-case) and can be edited; changes are audited.
- Doors that authenticate by IP don't need their key, but registration still returns one.
  Show it anyway (the door may support it later).
- Device list columns: `code`, `purpose`, building (doors), `sn` (tills), `allowed_ip`
  (doors), `online` (heard from in the last 2 minutes), `last_seen_at`, `last_ip`,
  `app_version`, `is_active`.
  `?online=false` lists devices needing attention.
- Changes to devices (including `allowed_ip`) are recorded in the audit log.

### Attendance

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/attendance/daily/` | `attendance.view` | One row per developer per day. Filters: `developer`, `work_date`, `date_from`, `date_to`, `status`, `department` |
| GET | `/attendance/records/` | `attendance.view` | Individual moments. Filters: `developer`, `work_date`, `date_from`, `date_to`, `source`, `event_type`, `is_void`, `device` |
| POST | `/attendance/records/` | `attendance.correct` | Manual record: `{developer, event_time, note, direction?}` (note required; `direction` `IN`/`OUT`, e.g. `OUT` to mark someone as gone who never scanned out) |
| GET | `/attendance/occupancy/` | `attendance.view` | **Who is inside right now**: `{as_of, total, buildings: [{id, code, name, count}], unknown_building}` |
| GET | `/attendance/occupancy/people/` | `attendance.view` | Everyone inside: `{developer, building, since, device_code}`. Filters: `building=<id>` (or `none`), `department`, `search` |
| POST | `/attendance/records/{id}/void/` | `attendance.correct` | `{reason}` (required) |
| GET | `/attendance/daily/me/`, `/attendance/records/me/` | logged in | Own attendance; same filters |

- **Occupancy is the main attendance view:** how many developers are inside each building
  and in total, and who they are. Worked hours (`/attendance/daily/`) still exist but are
  secondary.
- **Occupancy rule:** a developer is inside building X when their **latest** scan (on any
  day) is an `in` at X's door. **No scan out means still inside**, even on later days, until
  they scan again or a manager adds a manual `OUT` record. Everyone is counted once, so
  `total` = sum of the buildings + `unknown_building` (inside after a manual `IN` without a
  door). Deleted and terminated developers are never counted.
- Daily `status`: `PRESENT` or `INCOMPLETE` (a single scan, or an IN without an OUT).
  **No row means no scans that day**; absence and lateness aren't calculated yet.
- `worked_hours` is a ready-to-display number; `worked_seconds` is exact.
- Records have `direction` (what the door reported, or what a manager entered on a manual
  record) and `event_type` (the value used for attendance, normally the same). Display
  `event_type`; it can be recalculated if the rule changes, so never derive it yourself.
  `device_code` says which door (`Door1` = Building 1, `Door2` = Building 2).
- **Forgotten scan-out:** the correction form needs a `direction` choice (IN/OUT). An `OUT`
  record removes the developer from occupancy.
- Show voided records struck through, with `void_reason`, rather than hiding them.

### Sellers and service positions

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/sellers/` | `seller.view` | Filters: `status`, `has_user`. Search: name, contact, email |
| POST | `/sellers/` | `seller.create` | `{name, user?, contact_name?, email?, phone?, notes?}`; `user` = the seller's login |
| GET/PATCH | `/sellers/{id}/` | `seller.view` / `seller.update` | No DELETE: close with `status: "CLOSED"` |
| GET | `/sellers/me/` | logged in | Own seller profile (any status) |
| GET/POST | `/service-positions/` | `seller.view` / `seller.update`, or own seller | Filters: `seller`, `building`, `manager`, `is_active`. Fields include `building` (optional, `building_name`) and `manager` (user id, `manager_email`). Sellers omit `seller` on create; staff (e.g. ADMIN) must send it |
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

- **`kind`** (set on create, can't be changed later):
  - `PRODUCT`: tangible; optional stock (`track_stock`, `initial_quantity`, `/stock/`)
  - `SERVICE`: intangible and sold at the till (made-to-order coffee, haircut); no stock
  - `RENTAL`: booked by time slot (playground, pool); `price` is **per slot**. Requires a
    `rental` object; courts are booked through `/bookings/checkout/`, never added to a till purchase.
    ```json
    "rental": {"slot_minutes": 60, "opening_time": "08:00", "closing_time": "20:00",
               "weekdays": [0,1,2,3,4,5,6], "max_slots_per_booking": 3,
               "max_slots_per_day": 3, "max_days_ahead": 14}
    ```
    `weekdays`: 0 = Monday … 6 = Sunday. `max_slots_per_day` limits one developer's total
    slots on this rental per day, across all their bookings. A rental's `price` must be
    above 0. Times are company-local. Rental goods are created
    and edited with JSON (not multipart) because of the nested object.
- Filter the catalogue by `kind` (e.g. the till shows `kind=PRODUCT` and `kind=SERVICE`).
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
| GET | `/finance/accounts/` | `finance.view` | Filters: `status`, `developer`, `department`, `balance_min`, `balance_max`. Search: developer name, employee number. Ordering: `balance`, `developer__full_name`, `updated_at` |
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
| GET | `/purchases/detected-reader/` | own seller, or `purchase.create` | Readers connected to this PC: `{ip, reader, candidates}` (empty when none) |
| GET | `/purchases/readers/` | own seller, or `purchase.create` | All till readers: `[{code, name, online, last_seen_at}]` |
| POST | `/purchases/` | own seller, or `purchase.create` | `{service_position, reader?}`: opens a DRAFT bucket. Without `reader`, the reader connected to this PC is used (detected by address) |
| POST | `/purchases/{id}/reader/` | same | `{reader}`: switch the draft to another reader (a tap on the old one no longer counts) |
| POST | `/purchases/{id}/items/` | same | `{good, quantity?}`; adding a good already in the bucket increases its quantity |
| PATCH / DELETE | `/purchases/{id}/items/{item_id}/` | same | PATCH `{quantity}` / DELETE removes the line |
| POST | `/purchases/{id}/confirm/` | own seller, or `purchase.confirm` | `{pin}` + `Idempotency-Key`: charges the card tapped on the counter's reader |
| POST | `/purchases/{id}/cancel/` | own seller, or `purchase.cancel` | Drafts only |
| GET | `/purchases/`, `/purchases/{id}/` | own seller, or `purchase.view` | Filters: `kind` (`SALE` / `BOOKING`), `status`, `seller`, `service_position`, `developer`, `confirmed_after`, `confirmed_before`, `total_min`, `total_max` |
| GET | `/purchases/performance/` | own seller, or `purchase.view` | Sales per sell position (see *Building managers*) |
| GET | `/purchases/me/` | logged in | The developer's own purchases |
| POST | `/finance/accounts/me/pin/` | logged in | Developer sets `{pin}` or changes it with `{pin, current_pin}` |
| POST | `/finance/accounts/{id}/reset-pin/` | `finance.adjust` | Forgotten PIN: `{pin, pin_confirm}` typed by the developer replaces it; an empty body only clears it. Also clears any lockout |

Every item/confirm/cancel call returns the **whole purchase**, so re-render the bucket from
the response.

**Which till reader?** A till reader is plugged into the **seller's PC** and can be moved to
another PC, so it isn't tied to a counter. The server finds the reader **by the PC's network
address**: the reader program and the seller's browser run on the same PC, so they reach the
server from the same IP.

- `GET /purchases/detected-reader/` → `{ip, reader, candidates}`: the readers **connected to
  this PC** (same address, heard from in the last 2 minutes). Show them as the reader
  choice. **If none is connected, show an empty choice** ("No till reader connected to this
  PC") and don't offer taps. Refresh it when the till screen opens and every ~30 s.
- `POST /purchases/` without `reader`: if exactly one reader is connected, the purchase uses
  it. If none is connected yet, the **first tap from this PC's address** links the reader to
  the purchase. You may still send `reader` explicitly (e.g. two readers on one PC).
- A tap goes to the newest open purchase that names the reader, else to the newest open
  purchase created from the same address. The purchase keeps `reader` afterwards, as the
  record of which till was used.
- **Limitation:** this needs each PC to reach the server with its own address, which is
  true on the company LAN (production). It isn't true when many PCs share one router
  address (e.g. reaching the staging server over the internet): there, with several sellers
  active at once, a tap goes to the newer purchase. The PIN still prevents a wrong payment.

**Till flow:**
1. Seller picks their service position → `POST /purchases/` with `{service_position}` (plus
   `reader` if the screen chose one); keep the returned `id`.
2. Seller adds goods → `POST /purchases/{id}/items/`. `total` and `unit_price` show current prices.
3. The developer taps their card on the **till reader** of this PC. The reader sends the tap
   straight to the server (see `DEVICE_INTEGRATION.md`), which attaches it to this purchase
   (matched by reader or by this PC's address). **The web app never reads or sends card numbers.**
4. Wait for the tap: listen on the counter's WebSocket (section *Realtime* below) for
   `card_tapped`, or as a fallback poll `GET /purchases/{id}/` every ~1 s until
   `presented_card` is set:
   `{"developer": {...}, "presented_at": "...", "expires_at": "..."}`. Show
   "**Ada Lovelace** - enter your PIN". A newer tap replaces it; after `expires_at` it
   becomes `null` again (ask for another tap).
5. The developer types their PIN. Mask it, never store or log it, and clear it after each
   attempt. `POST /purchases/{id}/confirm/` with `{"pin": "..."}` and a **new**
   `Idempotency-Key` generated when the confirm step starts; reuse it only for automatic
   retries of that same attempt.
6. On `201`: show success with `developer.full_name`, `total` and `balance_after`
   (the developer's remaining balance), then start a new purchase.

**Confirmation is all-or-nothing.** Stock, the developer's balance and the purchase change
together, or nothing changes. On any error the purchase stays a DRAFT and can be retried:

| `code` | Show |
|---|---|
| `CARD_NOT_PRESENTED` | "Please tap your card" (no tap yet, or it expired) |
| `INVALID_PIN` | "Wrong PIN, N attempts left" (`details.attempts_remaining`) |
| `PIN_LOCKED` (423) | "PIN locked until …" (`details.locked_until`); another payment is needed |
| `PIN_NOT_SET` | "No PIN yet": the PIN is set when a card is assigned, or by finance (reset PIN with a new one) |
| `INSUFFICIENT_BALANCE` | Balance and amount needed (`details.balance`, `details.required`) |
| `INSUFFICIENT_STOCK` | Which good ran out (`details.good`, `available`) |
| `CARD_NOT_USABLE`, `DEVELOPER_NOT_ACTIVE`, `ACCOUNT_NOT_ACTIVE` | The `message` |

- Prices are fixed at confirmation: a confirmed purchase shows what was actually charged.
- **Confirmed purchases are final**; there are no refunds. Only drafts can be cancelled.
- Sellers can't charge their own card (`SELF_PURCHASE_FORBIDDEN`).
- `SELLER_NOT_ACTIVE`: the seller or service position was suspended or deactivated.

### Simulating card taps while developing (staging only)

You don't need a physical reader to build the till screen. On staging (these endpoints
answer `404` in production):

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/test-console/cards/?search=` | the purchase's seller, or `purchase.create` staff | Test developers: `{developer, full_name, card_uid, card_status, balance, account_status, has_pin}` (max 50) |
| POST | `/test-console/simulate-tap/` | same | `{"purchase": id, "developer": id}`, or `{"purchase": id, "uid": "04…"}` to test unknown or blocked cards |

The simulated tap goes through the **real** pipeline: it's recorded, attached to the
purchase, `card_tapped` is pushed on the counter's WebSocket, and the response is exactly
what a reader receives (`result`, `accepted`, `display_message`, `developer`, `purchase`).
So your screen reacts as it will in production. Then confirm with the PIN as usual.

- A purchase without a reader gets your **personal simulated reader** `SIM-<your user id>`,
  so testers don't receive each other's taps. A purchase that already names a reader keeps it.
- Demo developers' PIN on staging: ask the backend team (all demo developers share one).
- Good test cases: enough balance; too little balance (`INSUFFICIENT_BALANCE`); wrong PIN
  five times (`PIN_LOCKED`); `uid` of an unknown card (`UNKNOWN_CARD`); a blocked card;
  tapping a second card (the newest tap wins).
- To add a "Simulate tap" button to the till screen (development/staging only), follow
  `FRONTEND_TAP_SIMULATOR.md`: ready-to-paste server actions and component for this app.

### Rentals and bookings (playground desk)

Courts are booked **separately from goods**: a till purchase never contains a court, and a
booking checkout holds exactly one court. Developers **don't sign in**; they book at the
playground desk. The desk staff (the playground's seller account) pick court, date and
time, the developer **taps their card** on the desk's reader and **types their PIN**, and
the card holder gets the booking and pays for it. Until it starts, a paid booking can be
**moved** to another time or court.

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/rentals/` | logged in | Bookable courts with price per slot, `rental` rules, seller, location and images |
| GET | `/rentals/schedule/?date=YYYY-MM-DD` | logged in | **The day for every court**: booked and free periods, to find a blank time (optional `&search=tennis`) |
| GET | `/rentals/{id}/availability/?date=YYYY-MM-DD` | logged in | One court's slots: `{start, end, start_time, end_time, available, state}` |
| POST | `/bookings/checkout/` | own seller, or `purchase.create` | `{good, date, start_time, end_time, reader?}` → **201** the checkout (a purchase with `kind: "BOOKING"`, one court line, `total`, `presented_card`) |
| GET | `/bookings/checkout/{id}/` | own seller, or `purchase.view` | Poll it: `presented_card` shows who tapped |
| POST | `/bookings/checkout/{id}/confirm/` | own seller, or `purchase.confirm` | `{pin}` + `Idempotency-Key`: charges the card holder and **creates the booking** |
| POST | `/bookings/checkout/{id}/cancel/` | own seller, or `purchase.cancel` | Abandon before paying |
| GET | `/bookings/` | own seller, or `purchase.view` | Bookings of the seller's courts, each with `date`, `start_time`, `end_time`, `change_count`. Filters: `good`, `seller`, `developer`, `date`, `start_after`, `start_before` |
| POST | `/bookings/{id}/change/` | own seller, or `purchase.create` | `{date, start_time, end_time, good?}`: move a paid booking (see below) |

**Booking flow (desk screen):**
1. Pick a date and show `GET /rentals/schedule/?date=…`, so the developer can see the
   filled times of every court and choose a blank one.
2. `POST /bookings/checkout/` with `{good, date, start_time, end_time}`. Date and times
   are in the **company timezone** (`YYYY-MM-DD`, `HH:MM`) and must lie on the slot grid:
   with 60-minute slots 10:00–12:00 is 2 slots; 10:00–11:30 is `INVALID_SLOT`
   (`details.slot_minutes`). `reader` works as for till purchases (detected from the PC's
   address when left out). The response's `items[0]` has `date`, `start_time`, `end_time`,
   `quantity` (= slots) and `line_total` (= price × slots).
3. Show "Tap your card". The tap arrives exactly as at the till: the `card_tapped` event on
   the counter WebSocket (`ws/counters/<service_position>/`) or `presented_card` when you
   poll the checkout. *Simulate tap* in the test console works with the checkout id.
4. The developer types the PIN → `POST /bookings/checkout/{id}/confirm/`. On `201`, show
   the booking: court, `date`, `start_time`–`end_time`, `total`, `balance_after`.
5. To change the time before paying, cancel the checkout and start a new one.

- The time is **not held** while the checkout is open: if another desk confirms the same
  time first, this confirmation fails with `SLOT_UNAVAILABLE` and nothing is charged.
- Exclusive: nobody else can book an overlapping time on the same court, and **one
  developer can't hold two courts at the same time**. There are no refunds.

**Changing a paid booking** (`POST /bookings/{id}/change/`):

```json
{"date": "2026-10-03", "start_time": "14:00", "end_time": "16:00", "good": 9}
```

- Moves the booking to a new date / time, and optionally to **another court of the same
  seller** (`good`; leave out to keep the court). Returns the booking with
  `change_count` increased.
- Allowed **until the booking starts** (`BOOKING_STARTED` after that).
- **For now the new time must cost the same** as what was paid (same number of slots ×
  same price), so no money moves: otherwise `BOOKING_PRICE_DIFFERENT` with
  `details: {paid, new_price}`. (Charging/refunding a difference may come later.)
- The same rules as a new booking apply (free time, slot grid, opening hours, daily limit,
  one court at a time), ignoring the booking's own old time, so moving 10–12 to 11–13 works.
- The desk does it for the developer; no card tap or PIN is needed. Every change is in the
  audit log (`booking.changed`, old and new time).

**Day schedule** (`GET /rentals/schedule/?date=2026-10-02`). Each court's day is split into
periods; neighbouring slots with the same state are merged:

```json
{"date": "2026-10-02", "courts": [
  {"good": 7, "name": "Tennis court 1", "price": "15.00", "open": true,
   "opening_time": "08:00:00", "closing_time": "20:00:00", "slot_minutes": 60,
   "periods": [
     {"state": "FREE",   "start_time": "08:00", "end_time": "10:00", "start": "…", "end": "…"},
     {"state": "BOOKED", "start_time": "10:00", "end_time": "12:00", "start": "…", "end": "…"},
     {"state": "FREE",   "start_time": "12:00", "end_time": "20:00", "start": "…", "end": "…"}]}]}
```

| `state` | Meaning | Show |
|---|---|---|
| `FREE` | Can be booked | Selectable (e.g. green); a click can prefill `date`, `start_time`, `end_time` |
| `BOOKED` | Taken by a confirmed booking | Filled (e.g. grey); **who** booked is not shown |
| `PAST` | Already started | Disabled |
| `NOT_YET_OPEN` | Beyond `max_days_ahead` | Disabled, "bookable from …" |

`open: false` means the court is closed that weekday (`periods` is empty). Times are in the
company timezone. A FREE period may be longer than one booking allows; keep the selection
within `rental.max_slots_per_booking` slots. Drafts at other desks don't block a period, so
refresh the schedule after `SLOT_UNAVAILABLE`, or when a `purchase_confirmed` event arrives.

**Errors** (nothing is charged on any error):

| `code` | When | Show / do |
|---|---|---|
| `INVALID_SLOT` (400) | Times not on the grid (`details.slot_minutes`), outside opening hours, a closed day, in the past (also: the slot started while waiting), too far ahead, or too many slots | Reload the schedule; offer only grid times |
| `SLOT_UNAVAILABLE` (409) | Someone booked an overlapping time on this court first | "Just taken", reload the schedule |
| `ALREADY_BOOKED_THEN` (409) | The card holder already holds another court at that time; `details`: `booking`, `good`, `start`, `end` | "Ada already has *Football field* 10:00–12:00" |
| `DAILY_LIMIT_REACHED` (409) | Too many slots on this court that day for the card holder; `details`: `max_slots_per_day`, `already_booked`, `remaining` | "1 slot left today for Ada" |
| `RENTAL_NOT_AVAILABLE` (409) | The court was deactivated, has no rules, its seller is closed, or a change to another seller's court | Remove it from the list |
| `BOOKING_STARTED` / `BOOKING_PRICE_DIFFERENT` (409) | Changing a booking (see above) | Show `message` |
| `GOOD_NOT_AVAILABLE` (409) | A court sent to the till's `/items/`, or goods added to a booking checkout | Use the right screen |
| Till checkout errors | `CARD_NOT_PRESENTED`, `CARD_NOT_USABLE`, `INVALID_PIN`, `PIN_LOCKED`, `PIN_NOT_SET`, `INSUFFICIENT_BALANCE`, … | Same messages as the till |

- Generate a new `Idempotency-Key` when the PIN dialog opens; reuse it for retries of that
  same attempt only.
- A booking appears in the developer's statement like a till purchase, and in the seller's
  earnings. `/purchases/?kind=SALE` lists till sales only; `kind=BOOKING` booking payments.

### Realtime (WebSocket) for the till screen

The server pushes counter events, so the seller's screen updates the moment a card is
tapped, with no polling.

1. `POST /api/v1/realtime/ticket/` (normal auth) → `{"ticket": "...", "expires_in": 30}`.
   A ticket works **once** within 30 seconds; get a new one for every (re)connect.
2. Open `wss://<host>/ws/counters/<service_position_id>/?ticket=<ticket>`
   (`ws://` on a plain-HTTP setup). Allowed: the counter's own active seller, and users
   with `purchase.view`.
3. The first message is `{"type": "connected", "service_position": 3, "as": "user:7"}`.
   Every event has the shape `{"type", "service_position", "sent_at", "data"}`:

| `type` | `data` | Screen action |
|---|---|---|
| `card_tapped` | `result`, `accepted`, `display_message`, `developer`, `purchase` (id or null) | Sent to the counter of the open purchase using the tapped reader. Accepted with a purchase: show the name and ask for the PIN. Otherwise (e.g. blocked card) show `display_message` in red |
| `purchase_updated` | the full purchase (as `GET /purchases/{id}/`) | Re-render the bucket (useful for a second, customer-facing screen) |
| `purchase_confirmed` | the full purchase, incl. `total`, `balance_after`, `developer` | Show "Paid" and start a new purchase |
| `purchase_cancelled` | the full purchase | Clear the bucket |

- Close codes: `4401` bad or used ticket (get a new one), `4403` not your counter,
  `4404` unknown counter. Reconnect with backoff (1, 2, 5, 10 s) and **refetch the open
  purchase after every reconnect**, because events sent while disconnected are not replayed.
- Optional keep-alive: send `{"type": "ping"}` and receive `{"type": "pong"}`.
- With the BFF setup, the browser connects to the WebSocket directly (only `/ws/` needs to
  be reachable). Getting the ticket goes through your normal API path.

### Realtime attendance: live scans and building counts

**How it fits together:** door devices send scans to the server over plain HTTP. The
browser never talks to the doors. The server then **pushes** to every open dashboard
over this WebSocket, so the frontend learns about each scan instantly, without polling.

1. `POST /api/v1/realtime/ticket/` → `{"ticket": ...}` (user needs `attendance.view`).
2. Open `wss://<host>/ws/occupancy/?ticket=<ticket>`.
3. Messages:

| `type` | When | `data` |
|---|---|---|
| `occupancy` | Right after connecting, then after every scan or correction that changes who is inside | Same shape as `GET /attendance/occupancy/`: `{as_of, total, buildings: [{id, code, name, count}], unknown_building}` |
| `attendance` | **Every door scan**, accepted or not | `{id, result, accepted, direction, display_message, developer, device_code, building, event_time}` |

Example `attendance` message:

```json
{"type": "attendance", "data": {
  "id": 4711, "result": "ACCEPTED", "accepted": true, "direction": "IN",
  "display_message": "Welcome, Ada Lovelace",
  "developer": {"id": 1, "employee_number": "E0001", "full_name": "Ada Lovelace", "department": "Eng"},
  "device_code": "Door1", "building": {"id": 1, "code": "B1", "name": "Building 1"},
  "event_time": "2026-10-01T09:01:12Z"}}
```

- For an accepted scan, the `attendance` message arrives **first**, followed by the new
  `occupancy` counts. Rejected cards (`UNKNOWN_CARD`, `BLOCKED_CARD`, …) produce only the
  `attendance` message, with `developer` set to `null` for unknown cards. Show them in red.
- Use `attendance` for a live "who just came in or out" feed, and `occupancy` for the counters.
  Never compute counts yourself from the feed: just render the latest `occupancy` message.

Close codes: `4401` (bad or used ticket), `4403` (no `attendance.view`). Reconnect with
backoff; the first message after reconnecting is a fresh snapshot.

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

### Company statistics (BOSS dashboard)

`GET /stats/?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD` (`stats.view`: BOSS and ADMIN).
Both dates are company-local and optional: the default is the 30 days ending today; at
most 366 days. One request returns the whole dashboard:

```json
{
  "period": {"date_from": "2026-09-02", "date_to": "2026-10-01", "days": 30},
  "people": {
    "developers": {
      "total": 230,
      "by_status": {"ACTIVE": 221, "ON_LEAVE": 6, "SUSPENDED": 3, "TERMINATED": 12},
      "by_building": [{"building": 1, "name": "Building 1", "count": 120},
                      {"building": null, "name": null, "count": 4}]
    },
    "inside_now": {"total": 187, "buildings": [...], "unknown_building": 0, "as_of": "..."},
    "attendance": {
      "daily": [{"date": "2026-09-02", "present": 205, "avg_worked_hours": 8.12}, ...],
      "days_with_attendance": 22, "avg_present_per_day": 203.4, "avg_worked_hours": 8.05
    }
  },
  "money": {
    "developer_accounts": {"count": 230, "by_status": {"ACTIVE": 228, "FROZEN": 2},
                           "total_balance": "18420.00"},
    "deposits": {"total": "23000.00", "count": 230},
    "spending": {"total": "11870.50", "count": 2145},
    "daily": [{"date": "2026-09-02", "deposits": "0.00", "spending": "412.50"}, ...],
    "sellers": {"total_balance": "1530.00", "earnings": "11870.50", "payouts_paid": "10340.50",
                "payouts_pending": {"count": 2, "amount": "640.00"}}
  }
}
```

- `buildings` is `null` for BOSS / ADMIN (whole company). For a **building owner** it lists
  their buildings, and every figure is limited to them: their developers (home building)
  and those developers' money, the stores in their buildings (`store_sales`), and seller
  money only of stores entirely in their buildings.
- `money.store_sales`: confirmed till sales and court bookings at the positions in scope
  during the period: `{sales_total, sales_count, bookings_total, bookings_count}`.
- `daily` lists **every** day of the period (zeros included), ready for a chart.
- `developers.total` excludes terminated ones; `by_building` uses their home building
  (`null` = none set). `inside_now` is the live occupancy (same as `/attendance/occupancy/`).
- `present` = developers with attendance that day; `avg_worked_hours` is per present person.
  The overall averages only count days with attendance (weekends don't pull them down).
- Money is a string with 2 decimals (`CURRENCY`). `spending` = till purchases and bookings.
  `earnings` = sellers' sales in the period, `payouts_paid` = payouts paid in the period,
  `payouts_pending` = all payouts not yet paid (requested, approved, processing).
- The BOSS can open any list for details (developers, attendance, purchases, accounts,
  payouts, audit log, `/purchases/performance/` per store) but every change returns `403`.

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
| Devices (doors and till readers; register, key dialog, door IP, online status) | `rfid/devices/`, `rfid/devices/?online=false`, `rfid/devices/{id}/rotate-key/` | `rfid.view` (edit: `rfid.device.manage`) |
| Buildings (with **Owners** and **Managers** multi-selects) | `rfid/buildings/`, `users/?role=BUILDING_OWNER`, `users/?role=BUILDING_MANAGER` | `rfid.view` (edit: `rfid.device.manage`, i.e. ADMIN) |
| Live door feed | `attendance` messages on `ws/occupancy/` (or poll `rfid/events/?ordering=-event_time`) | `attendance.view` |
| **Occupancy dashboard** (live count per building and total, live scan feed; click a building for who is inside) | `ws/occupancy/`, `attendance/occupancy/people/?building=` | `attendance.view` |
| Attendance records and corrections (incl. "mark as left") | `attendance/records/`, `attendance/daily/` | `attendance.view` (corrections: `attendance.correct`) |
| Users and roles | `users/`, `roles/` | `user.view` |
| **Statistics dashboard** (BOSS: whole company; building owner: their buildings, named from `buildings`) | `stats/?date_from=&date_to=`, `purchases/performance/` | `stats.view` |
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
| Playground desk: day schedule → book a court (card tap + PIN) → change a booking | `rentals/schedule/`, `bookings/checkout/`, `bookings/checkout/{id}/confirm/`, `bookings/{id}/change/` | the playground's seller, or `purchase.create` + `purchase.confirm` |
| Rental schedule (seller) | `bookings/?date=` | active seller, or `purchase.view` |
| Audit log | `audit-logs/` | `audit.view` |

Build the navigation from `me.permissions` so each user only sees their pages.

---

## 8. Local development

1. Point the frontend at staging: `NEXT_PUBLIC_API_URL` (or a server-only `API_URL` for the
   BFF setup) = `https://<staging-host>:8443`.
2. The staging certificate is self-signed. For server-side calls from Node during
   development, trust it by setting `NODE_EXTRA_CA_CERTS=/path/to/staging.crt` (ask the
   backend team for the file). Don't use `NODE_TLS_REJECT_UNAUTHORIZED=0`.
3. Demo accounts exist for each role (`admin@demo.local`, `boss@demo.local`, `manager@demo.local`,
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
- nginx serves one hostname: `/api/`, `/admin/`, `/static/`, `/media/`, `/health/` go to
  Django, **`/ws/` goes to the backend's WebSocket service** (with the HTTP upgrade
  headers), and everything else goes to Next.js. Same origin means no CORS. Without the
  `/ws/` route the till screen and occupancy dashboard lose their live updates.
- Coordinate with the backend team to add the Next.js service to the offline install
  bundle.

---

## 10. Not built yet

These APIs are still to come; don't build screens against guesses. Permission codes for
them already exist in `me.permissions`.

- Generic approvals (e.g. for large deposits or adjustments)

Ask the backend team for the current state before starting on any of these.
