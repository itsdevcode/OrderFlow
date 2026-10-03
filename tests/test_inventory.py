from httpx import AsyncClient
from app.models.user import User
from typing import cast
import asyncio

async def login_and_get_access_token(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return cast(str, response.json()["access_token"])

async def test_inventory_crud_and_rbac(client: AsyncClient, admin_user: User, customer_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    customer_token = await login_and_get_access_token(client, customer_user.email, "CustomerPassword123!")

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    customer_headers = {"Authorization": f"Bearer {customer_token}"}

    # Setup category, product, warehouse
    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "ICat", "slug": "icat"})
    cat_id = cast(int, cat_resp.json()["id"])

    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "IProd", "slug": "iprod", "sku": "ISKU", "price": 10.0, "category_id": cat_id
    })
    prod_id = cast(int, prod_resp.json()["id"])

    wh_resp = await client.post("/api/v1/warehouses", headers=admin_headers, json={"code": "WH_I", "name": "Warehouse I"})
    wh_id = cast(int, wh_resp.json()["id"])

    # 1. Test adjust stock by admin
    adj_resp = await client.post(
        f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust",
        headers=admin_headers,
        json={"available_quantity_change": 10, "reserved_quantity_change": 2}
    )
    assert adj_resp.status_code == 200
    data = cast(dict[str, object], adj_resp.json())
    assert data["available_quantity"] == 10
    assert data["reserved_quantity"] == 2

    # 2. Test adjust stock by customer (denied)
    adj_resp2 = await client.post(
        f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust",
        headers=customer_headers,
        json={"available_quantity_change": 5}
    )
    assert adj_resp2.status_code == 403

    # 3. Test read stock by customer (allowed)
    get_resp = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=customer_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["available_quantity"] == 10

    list_resp = await client.get(f"/api/v1/inventory/products/{prod_id}", headers=customer_headers)
    assert list_resp.status_code == 200
    data = cast(list[dict[str, object]], list_resp.json())
    assert len(data) == 1

    # 4. Test negative constraints
    bad_adj = await client.post(
        f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust",
        headers=admin_headers,
        json={"available_quantity_change": -20}
    )
    assert bad_adj.status_code == 409  # InsufficientStockError

    # 5. Invalid product/warehouse
    bad_wh_adj = await client.post(
        f"/api/v1/inventory/products/{prod_id}/warehouses/9999/adjust",
        headers=admin_headers,
        json={"available_quantity_change": 5}
    )
    assert bad_wh_adj.status_code == 404  # WarehouseNotFoundError

async def test_inventory_concurrent_adjustment(client: AsyncClient, admin_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "CCat", "slug": "ccat"})
    cat_id = cast(int, cat_resp.json()["id"])
    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "CProd", "slug": "cprod", "sku": "CSKU", "price": 10.0, "category_id": cat_id
    })
    prod_id = cast(int, prod_resp.json()["id"])
    wh_resp = await client.post("/api/v1/warehouses", headers=admin_headers, json={"code": "WH_C", "name": "Warehouse C"})
    wh_id = cast(int, wh_resp.json()["id"])

    async def make_request():
        return await client.post(
            f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust",
            headers=admin_headers,
            json={"available_quantity_change": 2}
        )

    responses = await asyncio.gather(*(make_request() for _ in range(5)))
    for r in responses:
        assert r.status_code == 200

    get_resp = await client.get(f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}", headers=admin_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["available_quantity"] == 10

async def test_inventory_rollback_on_negative_adjustment(client: AsyncClient, admin_user: User) -> None:
    admin_token = await login_and_get_access_token(client, admin_user.email, "AdminPassword123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    cat_resp = await client.post("/api/v1/categories", headers=admin_headers, json={"name": "RBCat", "slug": "rbcat"})
    cat_id = cast(int, cat_resp.json()["id"])
    prod_resp = await client.post("/api/v1/products", headers=admin_headers, json={
        "name": "RBProd", "slug": "rbprod", "sku": "RBSKU", "price": 10.0, "category_id": cat_id
    })
    prod_id = cast(int, prod_resp.json()["id"])
    wh_resp = await client.post("/api/v1/warehouses", headers=admin_headers, json={"code": "WH_RB", "name": "Warehouse RB"})
    wh_id = cast(int, wh_resp.json()["id"])

    # Initial valid adjustment
    await client.post(
        f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust",
        headers=admin_headers,
        json={"available_quantity_change": 10}
    )

    # Failed negative adjustment
    bad_resp = await client.post(
        f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust",
        headers=admin_headers,
        json={"available_quantity_change": -20}
    )
    assert bad_resp.status_code == 409

    # Next valid adjustment (should succeed, lock must have been released)
    good_resp = await client.post(
        f"/api/v1/inventory/products/{prod_id}/warehouses/{wh_id}/adjust",
        headers=admin_headers,
        json={"available_quantity_change": 5}
    )
    assert good_resp.status_code == 200
    assert good_resp.json()["available_quantity"] == 15
