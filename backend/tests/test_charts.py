"""Planner output for known fixtures + every aggregate mode + every chart builder."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import charting
import planner
import profiling
from profiling import CAT, DATE, NUM

pytestmark_asyncio = pytest.mark.asyncio


@pytest.fixture(scope="module")
def fixture_frame():
    rng = np.random.default_rng(7)
    n = 240
    df = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n, freq="D"),
        "region": rng.choice(["North", "South", "East", "West"], n),
        "channel": rng.choice(["Retail", "Online", "Partner"], n),
        "revenue": rng.gamma(4, 250, n).round(2),
        "units": rng.integers(1, 60, n),
        "margin": rng.normal(0.3, 0.05, n).round(3),
        "note": [f"free text row {i} with some words" for i in range(n)],
    })
    types = {"date": DATE, "region": CAT, "channel": CAT, "revenue": NUM, "units": NUM,
             "margin": NUM, "note": profiling.TEXT}
    return df, types


def test_planner_produces_a_rich_deck(fixture_frame):
    df, types = fixture_frame
    slides = planner.plan(df, types, "Fixture", limit=40)
    assert len(slides) >= 20
    kinds = {s["type"] for s in slides}
    for expected in ("bar", "line", "scatter", "histogram", "heatmap", "treemap", "kpi"):
        assert expected in kinds, f"{expected} missing from {sorted(kinds)}"
    for s in slides:
        assert s["id"] and s["title"] and s["insight"] and s["spec"]["type"] == s["type"]
        assert isinstance(s["data"], dict)


def test_planner_is_deterministic(fixture_frame):
    df, types = fixture_frame
    a = [s["type"] for s in planner.plan(df, types, "F", limit=15)]
    b = [s["type"] for s in planner.plan(df, types, "F", limit=15)]
    assert a == b


def test_planner_respects_limit(fixture_frame):
    df, types = fixture_frame
    assert len(planner.plan(df, types, "F", limit=6)) == 6


def test_planner_returns_nothing_without_chartable_columns():
    df = pd.DataFrame({"a": ["long unique sentence %d" % i for i in range(50)]})
    types = {"a": profiling.TEXT}
    assert planner.plan(df, types, "S") == [] or all(
        s["type"] == "wordcloud" for s in planner.plan(df, types, "S"))


@pytest.mark.parametrize("agg,expected", [
    ("sum", 60.0), ("mean", 20.0), ("median", 20.0), ("min", 10.0), ("max", 30.0),
    ("count", 3.0),
])
def test_every_aggregate_mode(agg, expected):
    df = pd.DataFrame({"g": ["a", "a", "a", "b"], "v": [10.0, 20.0, 30.0, 5.0]})
    types = {"g": CAT, "v": NUM}
    data = charting.build(df, types, {"type": "bar", "x": "g", "y": "v", "agg": agg})
    labels = data["labels"]
    values = data["datasets"][0]["data"]
    got = dict(zip(labels, values))
    assert pytest.approx(got["a"], rel=1e-6) == expected


def test_groupby_agg_caps_categories():
    df = pd.DataFrame({"g": [f"c{i}" for i in range(200)], "v": range(200)})
    labels, values = charting.groupby_agg(df, "g", "v", "sum", limit=10)
    assert len(labels) <= 10 and len(values) == len(labels)


ALL_TYPES = sorted(charting.BUILDERS)


def test_every_chart_type_builds(fixture_frame):
    """All 37 builders must produce a payload for the fixture (via planner specs)."""
    df, types = fixture_frame
    built, failed = {}, []
    for t in ALL_TYPES:
        spec = _spec_for(t)
        try:
            built[t] = charting.build(df, types, dict(spec))
        except Exception as exc:  # noqa: BLE001
            failed.append((t, str(exc)))
    assert not failed, failed
    assert len(built) == 37


def _spec_for(t: str) -> dict:
    base = {"type": t}
    if t in ("bar", "horizontal_bar", "pie", "donut", "gauge", "radar", "polar_area",
             "radial_bar", "funnel", "waterfall", "pareto", "treemap"):
        return base | {"x": "region", "y": "revenue", "agg": "sum"}
    if t in ("grouped_bar", "stacked_bar", "sunburst", "sankey"):
        return base | {"x": "region", "y": "revenue", "series": "channel", "agg": "sum"}
    if t in ("line", "area", "step_line", "calendar"):
        return base | {"x": "date", "y": "revenue", "agg": "sum"}
    if t in ("multi_line", "stacked_area"):
        return base | {"x": "date", "y": "revenue", "series": "channel", "agg": "sum"}
    if t == "candlestick":
        return base | {"x": "date", "y": "revenue"}
    if t in ("scatter", "regression"):
        return base | {"x": "units", "y": "revenue"}
    if t == "bubble":
        return base | {"x": "units", "y": "revenue", "size": "margin"}
    if t in ("scatter3d", "surface3d"):
        return base | {"x": "units", "y": "margin", "z": "revenue"}
    if t == "bar3d":
        return base | {"x": "region", "y": "channel", "z": "revenue", "agg": "sum"}
    if t in ("histogram", "kde"):
        return base | {"x": "revenue"}
    if t in ("box", "violin"):
        return base | {"x": "region", "y": "revenue"}
    if t == "heatmap":
        return base | {"ys": ["revenue", "units", "margin"]}
    if t == "parallel":
        return base | {"ys": ["revenue", "units", "margin"], "series": "region"}
    if t == "kpi":
        return base | {"ys": ["revenue", "units"], "x": "date"}
    if t == "wordcloud":
        return base | {"x": "note"}
    return base | {"x": "region", "y": "revenue", "agg": "sum"}


def test_unknown_chart_type_raises(fixture_frame):
    df, types = fixture_frame
    with pytest.raises(charting.ChartError):
        charting.build(df, types, {"type": "hologram", "x": "region"})


def test_missing_column_raises(fixture_frame):
    df, types = fixture_frame
    with pytest.raises(charting.ChartError):
        charting.build(df, types, {"type": "bar", "x": "nope", "y": "revenue"})
