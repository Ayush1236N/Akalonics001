from __future__ import annotations

import pytest

from conftest import new_guest

pytestmark = pytest.mark.asyncio

CRED = {"email": "alice@example.com", "password": "s3cret-password"}


async def test_signup_login_me_logout(client):
    r = await client.post("/api/auth/signup", json=CRED)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["email"] == CRED["email"]
    assert body["user"]["is_guest"] is False
    assert body["access_token"] and body["refresh_token"] and body["csrf_token"]

    dup = await client.post("/api/auth/signup", json=CRED)
    assert dup.status_code == 409

    bad = await client.post("/api/auth/login", json={**CRED, "password": "wrong-password"})
    assert bad.status_code == 401

    ok = await client.post("/api/auth/login", json=CRED)
    assert ok.status_code == 200
    token = ok.json()["access_token"]
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["user"]["email"] == CRED["email"]

    out = await client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert out.status_code == 200
    reuse = await client.post("/api/auth/refresh",
                              json={"refresh_token": ok.json()["refresh_token"]})
    assert reuse.status_code == 401  # refresh tokens revoked by logout


async def test_password_is_hashed_with_argon2(client):
    from sqlalchemy import select

    from db import session_scope
    from models import User
    await client.post("/api/auth/signup", json={"email": "hash@example.com",
                                                "password": "another-password"})
    async with session_scope() as s:
        u = (await s.execute(select(User).where(User.email == "hash@example.com"))).scalar_one()
    assert u.password_hash.startswith("$argon2")
    assert "another-password" not in u.password_hash


async def test_weak_password_rejected(client):
    r = await client.post("/api/auth/signup", json={"email": "b@example.com", "password": "short"})
    assert r.status_code == 422


async def test_refresh_rotates_and_old_token_dies(client):
    s = await client.post("/api/auth/signup", json={"email": "rot@example.com",
                                                    "password": "rotate-me-please"})
    first = s.json()["refresh_token"]
    r1 = await client.post("/api/auth/refresh", json={"refresh_token": first})
    assert r1.status_code == 200
    second = r1.json()["refresh_token"]
    assert second != first
    assert (await client.post("/api/auth/refresh", json={"refresh_token": first})).status_code == 401
    assert (await client.post("/api/auth/refresh", json={"refresh_token": second})).status_code == 200


async def test_guest_mode_can_load_sample_instantly(client):
    g = await new_guest(client)
    assert g["user"]["is_guest"] is True
    r = await client.post("/api/sample/sales.xlsx", headers=g["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["sheets"] and r.json()["sheets"][0]["rows"] > 0


async def test_protected_routes_require_auth(client):
    for method, path in (("get", "/api/samples"), ("get", "/api/chart-types"),
                         ("get", "/api/rows/whatever"), ("post", "/api/analyze"),
                         ("post", "/api/nl-chart"), ("get", "/api/export/x")):
        r = await getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 401, f"{path} -> {r.status_code}"


async def test_public_routes_are_open(client):
    for path in ("/api/health", "/health", "/api/ready", "/api/config"):
        assert (await client.get(path)).status_code in (200, 503)


async def test_every_api_route_is_guarded_by_get_current_user():
    """Structural proof: no /api route escapes the auth dependency."""
    from app import api, app
    from auth import get_current_user

    allow = {"/api/health", "/api/ready", "/api/config", "/health", "/ready"}
    router_deps = [getattr(d, "dependency", None) for d in api.dependencies]
    assert get_current_user in router_deps
    covered = {r.path for r in api.routes}
    paths = [p for p in app.openapi()["paths"] if p.startswith("/api")]
    assert len(paths) > 15
    for path in paths:
        if path in allow or path.startswith("/api/auth"):
            continue
        assert path in covered, f"{path} is outside the guarded router"


async def test_cookie_auth_requires_csrf_double_submit(client):
    """Cookie-authenticated writes must carry the matching CSRF header."""
    r = await client.post("/api/auth/signup", json={"email": "csrf@example.com",
                                                    "password": "csrf-password-1"})
    csrf = r.json()["csrf_token"]
    # cookies are on the client jar now; no Authorization header => cookie auth
    bad = await client.post("/api/analyze", json={"dataset_id": "nope"})
    assert bad.status_code == 403
    good = await client.post("/api/analyze", json={"dataset_id": "nope"},
                             headers={"x-csrf-token": csrf})
    assert good.status_code == 404  # passed CSRF, failed ownership lookup
    client.cookies.clear()


async def test_api_key_auth(client):
    import secrets

    from sqlalchemy import select

    from db import session_scope
    from models import ApiKey, User
    from security import sha256

    await client.post("/api/auth/signup", json={"email": "key@example.com",
                                                "password": "api-key-password"})
    raw = secrets.token_urlsafe(24)
    async with session_scope() as s:
        u = (await s.execute(select(User).where(User.email == "key@example.com"))).scalar_one()
        s.add(ApiKey(user_id=u.id, name="ci", key_hash=sha256(raw)))
    client.cookies.clear()
    r = await client.get("/api/auth/me", headers={"x-api-key": raw})
    assert r.status_code == 200 and r.json()["user"]["email"] == "key@example.com"
