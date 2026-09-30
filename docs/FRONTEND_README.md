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
| POST | `/api/v1/auth/logout/` | `{refresh}` | 204 (refresh token revoked) |
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
| `finance.*`, `purchase.*`, `seller.*`, `good.*` | Reserved for modules not built yet |

Default roles, which admins can change:

| Role | Gets |
|---|---|
| BOSS | Everything |
| MANAGER | Developers, RFID, attendance, user list, audit log, seller/goods view |
| FINANCE_MANAGER | Finance, developer view, purchase view, audit log |
| SELLER_MANAGER | Sellers, goods, purchase view |
| DEVELOPER | Nothing global: only their own data via `/me/` endpoints |
| SELLER | Nothing global yet (own goods and sales come with the seller module) |

If `permissions` is empty, the user only has self-service pages (section 7).

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
`INVALID_CARD_TRANSITION`, `RECORD_ALREADY_VOID`, `CONFLICT`.

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

- Sellers, service positions, goods and goods images, stock movements
- Developer accounts, balances, deposits, ledger
- Purchases at the till (seller scans the developer's card), refunds
- Seller accounts and payouts, approvals

Ask the backend team for the current state before starting on any of these.
