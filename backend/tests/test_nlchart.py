"""NL chart: strict validation, one retry, keyword fallback, never eval."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

import nlchart
from profiling import CAT, DATE, NUM, TEXT

pytestmark = pytest.mark.asyncio


@pytest.fixture
def frame():
    rng = np.random.default_rng(11)
    n = 120
    df = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n, freq="D"),
        "region": rng.choice(["North", "South", "East", "West"], n),
        "channel": rng.choice(["Retail", "Online"], n),
        "revenue": rng.gamma(4, 300, n).round(2),
        "units": rng.integers(1, 50, n),
        "note": [f"comment {i}" for i in range(n)],
    })
    types = {"date": DATE, "region": CAT, "channel": CAT, "revenue": NUM, "units": NUM,
             "note": TEXT}
    return df, types


# --- validation -------------------------------------------------------------
def test_valid_spec_passes(frame):
    df, _types = frame
    spec = nlchart.validate_spec({"type": "treemap", "x": "region", "y": "revenue",
                                  "agg": "sum"}, list(df.columns))
    assert spec.type == "treemap" and spec.agg == "sum"


def test_unknown_chart_type_rejected(frame):
    df, _ = frame
    with pytest.raises(ValueError, match="supported chart types"):
        nlchart.validate_spec({"type": "hologram", "x": "region"}, list(df.columns))


def test_hallucinated_column_rejected(frame):
    df, _ = frame
    with pytest.raises(ValueError, match="not a column"):
        nlchart.validate_spec({"type": "bar", "x": "profit_center", "y": "revenue"},
                              list(df.columns))


def test_extra_fields_rejected(frame):
    df, _ = frame
    with pytest.raises(ValueError):
        nlchart.validate_spec({"type": "bar", "x": "region", "exec": "os.system('rm -rf /')"},
                              list(df.columns))


def test_agg_aliases_and_bad_agg(frame):
    df, _ = frame
    assert nlchart.validate_spec({"type": "bar", "x": "region", "y": "revenue",
                                  "agg": "average"}, list(df.columns)).agg == "mean"
    with pytest.raises(ValueError):
        nlchart.validate_spec({"type": "bar", "x": "region", "agg": "eval"}, list(df.columns))


def test_all_37_type_names_accepted(frame):
    df, _ = frame
    assert len(nlchart.CHART_TYPES) == 37
    for t in nlchart.CHART_TYPES:
        assert nlchart.validate_spec({"type": t}, list(df.columns)).type == t


def test_extract_json_handles_fences_and_prose():
    assert nlchart.extract_json('```json\n{"type":"bar"}\n```')["type"] == "bar"
    assert nlchart.extract_json('Sure!\n{"type":"pie"}\nHope that helps')["type"] == "pie"
    with pytest.raises(ValueError):
        nlchart.extract_json("no json here")


# --- LLM path with a stub caller -------------------------------------------
async def test_llm_first_attempt_success(frame):
    df, types = frame
    calls = []

    async def caller(messages):
        calls.append(messages)
        return json.dumps({"type": "treemap", "x": "region", "y": "revenue", "agg": "sum"})

    spec, source, warning = await nlchart.propose_spec("revenue by region as a treemap", df,
                                                       types, "S", call=caller)
    assert source == "llm" and warning is None and len(calls) == 1
    assert spec["type"] == "treemap"


async def test_llm_retries_once_with_validation_error(frame):
    df, types = frame
    seen = []

    async def caller(messages):
        seen.append(messages)
        if len(seen) == 1:
            return json.dumps({"type": "bar", "x": "not_a_column", "y": "revenue"})
        return json.dumps({"type": "bar", "x": "region", "y": "revenue", "agg": "sum"})

    spec, source, _w = await nlchart.propose_spec("revenue by region", df, types, "S",
                                                 call=caller)
    assert source == "llm_retry" and len(seen) == 2
    assert "not a column" in json.dumps(seen[1][-1]["content"])
    assert spec["x"] == "region"


async def test_falls_back_to_keyword_matcher_after_two_bad_replies(frame):
    df, types = frame

    async def caller(messages):
        return "I refuse to answer."

    spec, source, warning = await nlchart.propose_spec("revenue by region as a treemap", df,
                                                      types, "S", call=caller)
    assert source == "keyword" and warning and "rejected" in warning
    assert spec["type"] == "treemap"


async def test_llm_disabled_uses_keyword_only(frame, monkeypatch):
    df, types = frame
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    spec, source, warning = await nlchart.propose_spec("units over date as a line", df, types, "S")
    assert source == "keyword" and warning is None and spec["type"] == "line"


# --- keyword matcher --------------------------------------------------------
@pytest.mark.parametrize("prompt,expected", [
    ("show revenue by region as a treemap", "treemap"),
    ("revenue over date", "line"),
    ("units vs revenue", "scatter"),
    ("distribution of revenue", "histogram"),
    ("share of revenue by channel as a pie", "pie"),
    ("correlation heatmap", "heatmap"),
    ("ranking of region by revenue", "horizontal_bar"),
    ("word cloud of note", "wordcloud"),
    ("stacked bar of revenue by region", "stacked_bar"),
    ("waterfall of revenue by region", "waterfall"),
])
def test_keyword_matcher_types(frame, prompt, expected):
    df, types = frame
    assert nlchart.keyword_spec(prompt, df, types)["type"] == expected


def test_keyword_matcher_picks_mentioned_columns(frame):
    df, types = frame
    spec = nlchart.keyword_spec("total units by channel", df, types)
    assert spec["x"] == "channel" and spec["y"] == "units" and spec["agg"] == "sum"


def test_keyword_specs_always_build(frame):
    df, types = frame
    prompts = ["revenue by region", "units over date", "revenue vs units",
               "average revenue by channel", "count of records by region",
               "word cloud of note", "correlation", "distribution of units"]
    for p in prompts:
        spec = nlchart.keyword_spec(p, df, types)
        slide = nlchart.build_slide(dict(spec), df, types, "S", p)
        assert slide["data"] and slide["type"] == spec["type"]


def test_suggestions_reference_real_columns(frame):
    df, types = frame
    chips = nlchart.suggestions(df, types)
    assert 1 <= len(chips) <= 4
    assert any(c in " ".join(chips) for c in df.columns)


def test_schema_summary_is_compact(frame):
    df, types = frame
    summary = nlchart.schema_summary(df, types, "S")
    assert summary["rows"] == len(df)
    assert {c["name"] for c in summary["columns"]} == set(df.columns)
    assert len(json.dumps(summary)) < 6000


def test_build_slide_applies_filters(frame):
    df, types = frame
    slide = nlchart.build_slide(
        {"type": "bar", "x": "region", "y": "revenue", "agg": "sum",
         "filters": [{"column": "channel", "op": "==", "value": None},
                     {"column": "region", "op": "in", "value": ["North", "South"]}]},
        df, types, "S", "revenue by region for north and south")
    assert set(slide["data"]["labels"]) <= {"North", "South"}


# --- endpoint ---------------------------------------------------------------
async def test_nl_chart_endpoint_appends_to_deck(client, guest, dataset):
    body = {"dataset_id": dataset["dataset_id"], "sheet": dataset["sheets"][0]["name"],
            "prompt": "show revenue by region as a treemap"}
    r = await client.post("/api/nl-chart", json=body, headers=guest["headers"])
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["source"] in ("llm", "llm_retry", "keyword")
    assert payload["slide"]["type"] in nlchart.CHART_TYPES
    assert payload["llm"] is False  # no key configured in tests

    analyze = await client.post("/api/analyze", json={"dataset_id": dataset["dataset_id"],
                                                     "sheet": body["sheet"]},
                               headers=guest["headers"])
    ids = [s["id"] for s in analyze.json()["slides"]]
    assert payload["slide"]["id"] in ids  # appended to the persisted deck


async def test_nl_chart_validates_input(client, guest, dataset):
    r = await client.post("/api/nl-chart", json={"dataset_id": dataset["dataset_id"]},
                          headers=guest["headers"])
    assert r.status_code == 400
    r2 = await client.post("/api/nl-chart", json={"dataset_id": dataset["dataset_id"],
                                                 "prompt": "x" * 600}, headers=guest["headers"])
    assert r2.status_code == 422


async def test_nl_suggestions_endpoint(client, guest, dataset):
    r = await client.get(f"/api/nl-suggestions/{dataset['dataset_id']}", headers=guest["headers"])
    assert r.status_code == 200
    assert r.json()["suggestions"]


def test_no_eval_or_exec_in_module():
    """Guardrail: model output must never reach eval/exec/pickle."""
    src = (__import__("pathlib").Path(nlchart.__file__)).read_text()
    for bad in ("eval(", "exec(", "os.system", "subprocess", "pickle.loads"):
        assert bad not in src, bad


async def test_transport_failure_falls_back_to_keywords(frame):
    """A model/transport error on both attempts must degrade, never raise."""
    df, types = frame

    async def boom(_messages):
        raise RuntimeError("401 invalid or expired session token")

    spec, source, warning = await nlchart.propose_spec("units by region as a bar", df, types,
                                                       "Sheet1", call=boom)
    assert source == "keyword"
    assert warning and "rejected" in warning
    assert spec["type"] in nlchart.CHART_TYPES


async def test_server_errors_return_json_with_request_id(client, guest, dataset, monkeypatch):
    """Unhandled failures come back as JSON 500s (so CORS headers still apply)."""
    async def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(nlchart, "propose_spec", boom)
    r = await client.post("/api/nl-chart",
                          json={"dataset_id": dataset["dataset_id"], "prompt": "units by region"},
                          headers=guest["headers"])
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "Internal server error."
    assert body["request_id"] and r.headers["x-request-id"] == body["request_id"]
