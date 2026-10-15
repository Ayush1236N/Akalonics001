"""Demand Forecasting (T7) — sample/bundled data loading, all 7 models, ownership isolation."""
from __future__ import annotations

import pytest

from conftest import new_guest

pytestmark = pytest.mark.anyio


async def test_meta_lists_categories_models_bundled(client, guest):
    r = await client.get("/api/forecast/meta", headers=guest["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert {c["id"] for c in body["categories"]} == {"T7-F", "T7-SFG", "T7-R"}
    assert len(body["models"]) == 7
    assert {b["id"] for b in body["bundled"]} == {"dummy_1000", "test2_shirt"}


async def test_sample_load_and_run_all_seven_models(client, guest):
    r = await client.post("/api/forecast/sample/T7-F", headers=guest["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["count"] > 0

    r = await client.post("/api/forecast/run", headers=guest["headers"],
                          json={"category": "T7-F", "granularity": "monthly", "sku": "__all__"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["model"] for m in body["models"]] == \
        ["ses", "holt", "holt_winters", "auto_arima", "croston", "linreg", "gboost"]
    assert body["recommended"] in [m["model"] for m in body["models"]]
    # every model exposes a raw future-value array the same length as future_labels —
    # this is what the Future Demand table renders.
    for m in body["models"]:
        assert len(m["future"]) == len(body["future_labels"])
    assert "surface3d" in body  # category rollup with 2+ SKUs gets the Time x SKU surface


async def test_bundled_real_dataset_loads_and_forecasts(client, guest):
    r = await client.post("/api/forecast/bundled/dummy_1000", headers=guest["headers"],
                          json={"category": "T7-SFG"})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1000

    r = await client.post("/api/forecast/run", headers=guest["headers"],
                          json={"category": "T7-SFG", "granularity": "monthly", "sku": "__all__"})
    assert r.status_code == 200, r.text


async def test_bundled_unknown_name_rejected(client, guest):
    r = await client.post("/api/forecast/bundled/does-not-exist", headers=guest["headers"],
                          json={"category": "T7-F"})
    assert r.status_code == 404


async def test_manual_rows_validated(client, guest):
    r = await client.post("/api/forecast/data", headers=guest["headers"],
                          json={"category": "T7-R", "mode": "append",
                                "rows": [{"date": "2026-01-01", "sku": "X", "quantity": -1}]})
    assert r.status_code == 422

    r = await client.post("/api/forecast/data", headers=guest["headers"],
                          json={"category": "T7-R", "mode": "append",
                                "rows": [{"date": "2026-01-01", "sku": "X", "quantity": 5},
                                        {"date": "2026-01-02", "sku": "X", "quantity": 7}]})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2


async def test_run_without_data_is_a_clean_422(client, guest):
    r = await client.post("/api/forecast/run", headers=guest["headers"],
                          json={"category": "T7-SFG", "granularity": "weekly", "sku": "__all__"})
    assert r.status_code == 422


async def test_clear_removes_only_that_category(client, guest):
    await client.post("/api/forecast/sample/T7-F", headers=guest["headers"])
    await client.post("/api/forecast/sample/T7-SFG", headers=guest["headers"])

    r = await client.delete("/api/forecast/data/T7-F", headers=guest["headers"])
    assert r.status_code == 200
    assert r.json()["count"] == 0

    r = await client.get("/api/forecast/data/T7-SFG", headers=guest["headers"])
    assert r.json()["count"] > 0  # untouched


async def test_forecast_data_is_isolated_per_user(client, guest):
    await client.post("/api/forecast/sample/T7-F", headers=guest["headers"])

    other = await new_guest(client)
    r = await client.get("/api/forecast/data/T7-F", headers=other["headers"])
    assert r.json()["count"] == 0  # a fresh guest never sees another user's demand history


async def test_category_and_granularity_validated(client, guest):
    r = await client.post("/api/forecast/run", headers=guest["headers"],
                          json={"category": "NOT-A-CATEGORY", "granularity": "monthly"})
    assert r.status_code == 400

    await client.post("/api/forecast/sample/T7-F", headers=guest["headers"])
    r = await client.post("/api/forecast/run", headers=guest["headers"],
                          json={"category": "T7-F", "granularity": "not-a-granularity"})
    assert r.status_code == 400
