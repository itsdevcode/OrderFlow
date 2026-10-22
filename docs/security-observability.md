# Security and observability

[README](../README.md) · [Authentication workflow](business-workflows.md#authentication)

Security properties here describe the checked-in implementation. They do not
constitute a complete deployment security review.

## Authentication and password handling

[pwdlib](../app/core/security.py) uses `PasswordHash.recommended()` with the
installed Argon2 backend. Stored user rows contain password hashes. Login verifies
the hash and active flag; refresh records store token identity, expiry and
revocation, rather than a raw refresh JWT.

[JWT helpers](../app/core/jwt.py) sign access/refresh tokens with a configured
secret and algorithm (HS256 by default). Decoding supplies an allowed algorithm
list and verifies expiry. Access routes reject refresh token types; refresh/logout
reject access types and malformed subjects. The current implementation does not
require issuer/audience claims, manage signing-key rotation, maintain an access
JWT denylist or implement device/session management.

Refresh/logout use a per-user PostgreSQL NO KEY UPDATE lock before token reads,
with audited commits. Reuse of a revoked refresh token revokes all refresh records
for that user. Issued access tokens remain subject to their expiry; every protected
request also checks that the current user exists and is active.

`PAYMENT_WEBHOOK_SECRET` must be set when DEBUG is false; debug mode supplies an
insecure development fallback. Settings do not enforce JWT secret entropy or
reject every insecure operator choice. Compose requires nonempty signing-secret
variables, but operators must supply strong unique values. Default sample values
are for local development only.

## Authorization and validation

[CurrentUser](../app/dependencies/auth.py) resolves a valid active account.
[require_roles](../app/dependencies/permissions.py) checks ADMIN/MANAGER/CUSTOMER
roles loaded from PostgreSQL; a caller cannot elevate permissions by supplying a
JWT role claim. Pydantic schemas validate typed request bodies, bounded identifiers,
quantities and pagination where declared. Validation is schema-specific; it is
not a blanket guarantee of strict rejection of every extra field.

Ownership is enforced separately from roles: carts and order queries use the
current user's ID; payment/shipment services allow staff access to other users'
orders; notification reads are recipient-only. Non-owned resources commonly
return 404 to avoid confirming their existence. `/api/v1/users` is admin-only
provisioning, not open registration. See the [route matrix](api-reference.md).

## Admission and webhook trust

Login/refresh use a Redis-clock fixed-window IP limiter and fail closed with 503
if shared admission state is unavailable. Exhausted windows return 429 and an
integer Retry-After. These are IP-level buckets: NAT sharing and cross-boundary
bursts are known trade-offs; there is no account-lockout or MFA implementation.

The payment webhook is not a bearer-user route. It authenticates HMAC-SHA256 over
exact raw body bytes with constant-time comparison, bounds streamed body size,
validates schema and timestamp, and checks payment amount/currency against stored
values. Provider/event uniqueness plus payload hashing distinguish a duplicate
from conflicting event-ID reuse. An already processed byte-identical event can
be acknowledged beyond the normal 300-second window, safely supporting delayed
provider retries. Redis failure bypasses only webhook rate admission; it never
bypasses HMAC or database idempotency.

Trusted proxy CIDRs are empty by default. Forwarding metadata is only accepted
from explicitly trusted socket peers. Preserve the real peer with Uvicorn
`--no-proxy-headers`; configure TLS ingress and proxy ranges deliberately rather
than trusting arbitrary X-Forwarded-For headers.

## Structured logs and correlation

[RequestContextMiddleware](../app/middleware/request_context.py) accepts one valid
canonical UUID-shaped X-Request-ID or generates a new UUID. Duplicate, invalid,
oversized and noncanonical values are replaced. The ID is returned on responses
and held in a ContextVar reset after every request, including failures.

[JsonFormatter](../app/core/logging.py) emits a fixed JSON schema:

```json
{"timestamp":"2026-10-22T12:00:00+00:00","severity":"INFO","logger":"app.http","event":"http.request_completed","request_id":"11111111-1111-4111-8111-111111111111","http_method":"GET","path":"/api/v1/orders/{order_id}","status":200,"duration_ms":3.2}
```

This is an illustrative record. Paths are matched route templates, with
`<unmatched>` for unknown routes. Raw URLs, query strings, bodies, authorization
headers, log messages/arguments, arbitrary extras and exception payloads are
omitted. This omission policy is stronger and narrower than attempting regex
redaction of arbitrary message text: the sensitive text is never serialized by
this formatter. SQL parameter echo is disabled and SQLAlchemy engine exceptions
hide parameters. Redis sanitizers additionally mask credentials/URLs before
legacy messages reach logging.

Application and Uvicorn error loggers use the safe handler; default Uvicorn access
logs are disabled. Other library/root handlers are not globally replaced, so an
operator's additional handlers/exporters need their own privacy review. The
middleware preserves HTTP failure propagation and correlation; these minimal
records intentionally sacrifice exception detail. There is no metrics backend,
trace exporter, alerting deployment or log retention policy included.

## Transactional audit events

[AuditService](../app/services/audit.py) validates allowlisted numeric/status
metadata and stages an [AuditLog](../app/models/audit_log.py). Successful mandatory
staging never commits independently of its business owner. An audit insert or
business commit failure rolls back the critical mutation.

| Action | Recorded with |
| --- | --- |
| AUTH.LOGIN_SUCCEEDED | New persisted refresh identity |
| AUTH.TOKEN_REFRESHED | Old revocation and new refresh identity |
| AUTH.TOKEN_REUSE_DETECTED | All-user refresh revocation; failure outcome |
| AUTH.LOGOUT | Refresh revocation |
| ORDER.CREATED | Order, items, inventory reservations, cart clear and notification |
| INVENTORY.ADJUSTED | Before/after quantities and deltas |
| PAYMENT.SUCCEEDED / PAYMENT.FAILED | Transition audit; when webhook-owned, its wider transaction |
| AUTH.LOGIN_FAILED | Anonymous separate best-effort transaction with a bounded timeout |

Audits store action, actor when known, resource, outcome, request ID, timestamp
and allowlisted metadata. Failed login evidence excludes entered identity and
credentials; its unavailability does not override the authentication rejection.
Payment webhook actors are anonymous provider-origin operations. Actor deletion
sets actor_id to NULL, retaining history. Notifications and request logs serve
different purposes and are not substitutes for an audit row.

This is selected-event application auditing, not exhaustive change data capture.
Catalog and shipment mutations do not automatically receive audit events. Audits
have no immutable/WORM storage, database write-protection role, external signing,
retention enforcement or public audit query endpoint.

## Production limitations

- Provide TLS termination, explicit proxy trust, network exposure controls and
  rate-limit capacity/availability appropriate to the deployment.
- Rotate strong signing/webhook secrets; review JWT lifetime and access revocation
  requirements. Keep `.env` out of Git, images and incident attachments.
- Add backups and restore drills, database least-privilege roles, capacity planning,
  observability retention and dependency/image vulnerability review.
- The Compose password/service isolation is a local baseline; it does not supply
  managed secrets, PostgreSQL/Redis TLS, high availability or immutable image digests.
- Integrate a real payment provider with provider-side idempotency and explicit
  reconciliation/refund policy. Only a fake adapter is currently wired.
- In-process background tasks can be interrupted. Add an operational scheduler for
  eligible notifications/reservation expiry and reconciliation; external sends
  need provider deduplication for crash-after-send recovery.
- There is no MFA, password recovery flow, self-service registration, public order
  cancellation, automatic reservation expiry or production SLO claim.
