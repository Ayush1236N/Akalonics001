"""Synthetic demo history for the Demand Forecasting feature.

Lets a user click "Load sample data" per category instead of typing hundreds of rows
by hand before trying the module out. Not used for anything else in the app.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_RNG_SEED = {"T7-F": 701, "T7-SFG": 702, "T7-R": 703}
_DAYS = 900  # ~2.5 years of daily history, enough for monthly/quarterly seasonality checks

_SKUS = {
    "T7-F": [
        ("FG-JACKET-01", "seasonal"),
        ("FG-SNEAKER-02", "trending"),
        ("FG-TSHIRT-03", "stable"),
        ("FG-BUNDLE-04", "trending"),
    ],
    "T7-SFG": [
        ("SFG-PANEL-01", "trending"),
        ("SFG-MOTOR-02", "stable"),
        ("SFG-CHASSIS-03", "seasonal"),
        ("SFG-HARNESS-04", "intermittent"),
    ],
    "T7-R": [
        ("RM-STEEL-01", "stable"),
        ("RM-RESIN-02", "trending"),
        ("RM-SPAREPART-03", "intermittent"),
        ("RM-FABRIC-04", "seasonal"),
    ],
}


def _series_for(pattern: str, rng: np.random.Generator, n: int) -> np.ndarray:
    t = np.arange(n)
    if pattern == "stable":
        base = 40 + 3 * rng.standard_normal(n)
    elif pattern == "trending":
        base = 20 + 0.06 * t + 4 * rng.standard_normal(n)
    elif pattern == "seasonal":
        base = 60 + 25 * np.sin(2 * np.pi * t / 365 - 1.2) + 6 * rng.standard_normal(n)
    elif pattern == "intermittent":
        base = np.zeros(n)
        nz = rng.random(n) < 0.08
        base[nz] = rng.integers(3, 30, size=int(nz.sum()))
        return np.clip(base, 0, None).round().astype(float)
    else:
        base = 30 + 2 * rng.standard_normal(n)
    return np.clip(base, 0, None).round().astype(float)


def generate(category: str) -> pd.DataFrame:
    if category not in _SKUS:
        raise ValueError(f"Unknown category: {category}")
    rng = np.random.default_rng(_RNG_SEED[category])
    dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=_DAYS, freq="D")
    rows = []
    for sku, pattern in _SKUS[category]:
        qty = _series_for(pattern, rng, _DAYS)
        for d, q in zip(dates, qty):
            if q > 0:
                rows.append({"date": d, "sku": sku, "quantity": float(q)})
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Bundled real datasets — user-supplied history, shipped as CSVs alongside this
# module so "Load real sample data" has something besides synthetic fixtures.
# --------------------------------------------------------------------------- #
import pathlib

_SEED_DIR = pathlib.Path(__file__).resolve().parent / "forecast_seed_data"

BUNDLED = {
    "dummy_1000": {
        "label": "Demand history sample (1,000 rows)",
        "description": "Daily Date / Product ID / Actual demand across several finished-good SKUs.",
        "file": _SEED_DIR / "dummy_1000.csv",
    },
    "test2_shirt": {
        "label": "TEST2 — shirt production history",
        "description": "Daily July + August production quantities for a single SKU (SHIRT).",
        "file": _SEED_DIR / "test2_shirt.csv",
    },
}


def load_bundled(name: str) -> pd.DataFrame:
    if name not in BUNDLED:
        raise ValueError(f"Unknown bundled dataset: {name}")
    df = pd.read_csv(BUNDLED[name]["file"], parse_dates=["date"])
    return df.sort_values("date").reset_index(drop=True)
