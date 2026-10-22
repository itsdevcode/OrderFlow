# API reference

OrderFlow exposes JSON over HTTP. Interactive OpenAPI documentation is at
`/docs` while the application runs. This page records authentication,
authorization, and operational details that are easy to miss in generated docs.

## Conventions

- API prefix: `/api/v1`; health endpoints are `/health` and `/health/db`.
- Protected routes require `Authorization: Bearer <access-token>`.
- Access tokens last 15 minutes; refresh tokens last 7 days.
- Domain errors use FastAPI's `{"detail": "..."}` response shape.
- IDs are integers except token, provider, and webhook event identifiers.

## Authorization

| Area | Customer | Manager | Admin |
| --- | --- | --- | --- |
| Login, refresh, logout, own profile | Yes | Yes | Yes |
| Read catalog, warehouses, and inventory | Yes | Yes | Yes |
| Manage categories, products, warehouses, inventory | No | Yes | Yes |
| Create users | No | No | Yes |
| Own cart and orders | Yes | Yes | Yes |
| Initiate payment for own order | Yes | Yes | Yes |
| Initiate payment for another user's order | No | Yes | Yes |
| Read own shipment | Yes | Yes | Yes |
| Create, list, or update shipments | No | Yes | Yes |
| Read own notifications | Yes | Yes | Yes |

Reading another user's order or shipment returns `404`, avoiding disclosure of
whether that resource exists.

## Endpoint inventory

All catalog and business endpoints require authentication. Login, refresh, the
payment webhook, and health checks are exceptions.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/auth/login` | Exchange credentials for a token pair |
| `POST` | `/api/v1/auth/refresh` | Rotate a valid refresh token |
| `POST` | `/api/v1/auth/logout` | Revoke one refresh token |
| `GET` | `/api/v1/auth/me` | Read the current user |
| `POST` | `/api/v1/users` | Create a user (admin) |
| `POST`, `GET` | `/api/v1/categories` | Create or list categories |
| `GET`, `PATCH`, `DELETE` | `/api/v1/categories/{category_id}` | Read or manage a category |
| `POST`, `GET` | `/api/v1/products` | Create or list products |
| `GET`, `PATCH`, `DELETE` | `/api/v1/products/{product_id}` | Read or manage a product |
| `POST`, `GET` | `/api/v1/warehouses` | Create or list warehouses |
| `GET`, `PATCH`, `DELETE` | `/api/v1/warehouses/{warehouse_id}` | Read or manage a warehouse |
| `GET` | `/api/v1/inventory/products/{product_id}` | List stock by warehouse |
| `GET` | `/api/v1/inventory/products/{product_id}/warehouses/{warehouse_id}` | Read one stock record |
| `POST` | `/api/v1/inventory/products/{product_id}/warehouses/{warehouse_id}/adjust` | Adjust stock |
| `GET`, `DELETE` | `/api/v1/cart` | Read or clear the current user's cart |
| `POST` | `/api/v1/cart/items` | Add an item |
| `PATCH`, `DELETE` | `/api/v1/cart/items/{item_id}` | Change or remove an item |
| `POST`, `GET` | `/api/v1/orders` | Create an order or list own orders |
| `GET` | `/api/v1/orders/{order_id}` | Read an owned order |
| `POST`, `GET` | `/api/v1/orders/{order_id}/payments` | Initiate or list payments |
| `POST` | `/api/v1/webhooks/payments` | Receive a signed provider event |
| `POST`, `GET` | `/api/v1/orders/{order_id}/shipments` | Create or read an order shipment |
| `GET` | `/api/v1/shipments/{shipment_id}` | Read a shipment |
| `PATCH` | `/api/v1/shipments/{shipment_id}/status` | Update shipment state |
| `GET` | `/api/v1/shipments` | List shipments (manager/admin) |
| `GET` | `/api/v1/notifications` | List own notifications |
| `GET` | `/api/v1/notifications/{notification_id}` | Read an owned notification |

## Authentication example

```bash
curl -sS http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"replace-me"}'
```

The response contains `access_token`, `refresh_token`, and `token_type`. Refresh
rotation invalidates the submitted token. Reusing a revoked refresh token revokes
all refresh tokens for that user.

```bash
curl -sS http://localhost:8000/api/v1/auth/refresh \
  -H 'Content-Type: application/json' \
  -d '{"refresh_token":"<refresh-token>"}'
```

## Cart and order example

```bash
curl -sS 'http://localhost:8000/api/v1/products?limit=20&offset=0' \
  -H 'Authorization: Bearer <access-token>'

curl -sS http://localhost:8000/api/v1/cart/items \
  -H 'Authorization: Bearer <access-token>' \
  -H 'Content-Type: application/json' \
  -d '{"product_id":42,"quantity":2}'

curl -sS -X POST http://localhost:8000/api/v1/orders \
  -H 'Authorization: Bearer <access-token>'
```

Order creation calculates current prices, selects one warehouse per cart item,
reserves inventory, clears the cart, and commits once.

## Payment example

```bash
curl -sS http://localhost:8000/api/v1/orders/123/payments \
  -H 'Authorization: Bearer <access-token>' \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"checkout-123-attempt-1","provider":"fake_provider"}'
```

The client supplies a globally unique idempotency key. Repeating a compatible
request returns the existing payment; reusing the key for another order or
provider returns `409`. The server calculates the amount from order snapshots.

The webhook signs `<timestamp>.<raw-body>` with HMAC-SHA256 using
`PAYMENT_WEBHOOK_SECRET` and requires `X-Webhook-Signature`,
`X-Webhook-Timestamp`, and `X-Webhook-Event-Id`. New events outside the
five-minute window are rejected. A stored, byte-identical event can be
acknowledged again safely.

## Shipment example

Managers and admins can create a shipment for a confirmed, successfully paid
order:

```bash
curl -sS http://localhost:8000/api/v1/orders/123/shipments \
  -H 'Authorization: Bearer <staff-access-token>' \
  -H 'Content-Type: application/json' \
  -d '{"carrier":"Example Express","tracking_number":"EX-123"}'
```

Allowed transitions are `PENDING` to `SHIPPED` or `CANCELLED`, and `SHIPPED` to
`DELIVERED`. Terminal states cannot transition again.

## Health endpoints

`GET /health` confirms the process can answer requests. `GET /health/db` executes
a database probe. Redis is optional at request time; cache and rate-limit
behavior follows the failure policies in [Reliability](reliability.md).

## Source of truth

FastAPI generates complete schemas at `/openapi.json`. When this guide and the
running schema differ, the deployed schema controls. See
[Business workflows](business-workflows.md) for transaction boundaries and
[Security and observability](security-observability.md) for trust boundaries.
