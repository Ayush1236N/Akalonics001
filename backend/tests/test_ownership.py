"""Ownership isolation: user A must never see user B's dataset — 404, not 403."""
from __future__ import annotations

import pytest

from conftest import new_guest

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def two_users(client):
    a = await new_guest(client)
    b = await new_guest(client)
    client.cookies.clear()  # guest fixture sets cookies; force header auth
    ds = await client.post("/api/sample/sales.xlsx", headers=b["headers"])
    assert ds.status_code == 200, ds.text
    return a, b, ds.json()


async def test_user_a_gets_404_on_user_b_dataset(client, two_users):
    a, _b, ds = two_users
    dsid = ds["dataset_id"]
    r = await client.get(f"/api/rows/{dsid}", headers=a["headers"])
    assert r.status_code == 404
    assert "not available" in r.json()["detail"]


async def test_every_read_path_is_ownership_scoped(client, two_users):
    a, b, ds = two_users
    dsid, sheet = ds["dataset_id"], ds["sheets"][0]["name"]
    calls = [
        ("get", f"/api/datasets/{dsid}", {}),
        ("get", f"/api/rows/{dsid}", {}),
        ("post", "/api/analyze", {"json": {"dataset_id": dsid}}),
        ("post", "/api/aggregate", {"json": {"dataset_id": dsid,
                                             "spec": {"type": "bar", "x": sheet}}}),
        ("post", "/api/describe", {"json": {"dataset_id": dsid}}),
        ("post", "/api/nl-chart", {"json": {"dataset_id": dsid, "prompt": "bar chart"}}),
        ("get", f"/api/export/{dsid}?format=pptx", {}),
        ("get", f"/api/export/pdf/{dsid}", {}),
        ("get", f"/api/export/png/{dsid}/bar-0", {}),
        ("post", "/api/jobs", {"json": {"dataset_id": dsid}}),
    ]
    for method, path, kw in calls:
        r = await getattr(client, method)(path, headers=a["headers"], **kw)
        assert r.status_code == 404, f"{method} {path} leaked with {r.status_code}"
    # ...and the owner still gets through
    ok = await client.get(f"/api/rows/{dsid}", headers=b["headers"])
    assert ok.status_code == 200


async def test_dataset_listing_only_shows_own(client, two_users):
    a, b, ds = two_users
    la = await client.get("/api/datasets", headers=a["headers"])
    lb = await client.get("/api/datasets", headers=b["headers"])
    assert la.status_code == 200 and lb.status_code == 200
    assert ds["dataset_id"] not in [d["dataset_id"] for d in la.json()["datasets"]]
    assert ds["dataset_id"] in [d["dataset_id"] for d in lb.json()["datasets"]]


async def test_jobs_are_ownership_scoped(client, two_users):
    a, b, _ds = two_users
    up = await client.post("/api/upload",
                           files={"file": ("survey.csv", open("../samples/survey.csv", "rb"),
                                           "text/csv")},
                           headers=b["headers"])
    assert up.status_code == 200, up.text
    jid = up.json()["job_id"]
    assert (await client.get(f"/api/jobs/{jid}", headers=a["headers"])).status_code == 404
    assert (await client.get(f"/api/jobs/{jid}", headers=b["headers"])).status_code == 200
