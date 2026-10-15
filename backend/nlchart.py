"""Natural-language chart specs.

Pipeline: schema summary -> LLM strict JSON -> pydantic + real-column validation.
On invalid output retry once with the validation error, then fall back to a pure
keyword matcher. Model output is never eval'd or exec'd: it is parsed as JSON and
every field is checked against the actual sheet.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

import charting
import planner
import profiling
from config import settings

CHART_TYPES = tuple(charting.CHART_META.keys())
AGGS = ("sum", "mean", "median", "count", "min", "max")


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str
    op: Literal["in", "contains", ">", ">=", "<", "<=", "=="]
    value: Any = None


class ChartSpec(BaseModel):
    """Strict shape mirroring the planner spec."""
    model_config = ConfigDict(extra="forbid")

    type: str
    x: str | None = None
    y: str | None = None
    series: str | None = None
    agg: str | None = None
    title: str | None = Field(default=None, max_length=200)
    filters: list[Filter] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        v = (v or "").strip().lower().replace(" ", "_").replace("-", "_")
        if v not in CHART_TYPES:
            raise ValueError(f"type must be one of the {len(CHART_TYPES)} supported chart types")
        return v

    @field_validator("agg")
    @classmethod
    def _known_agg(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        v = v.strip().lower()
        if v in ("avg", "average"):
            v = "mean"
        if v not in AGGS:
            raise ValueError(f"agg must be one of {AGGS}")
        return v


def validate_spec(payload: dict, columns: list[str]) -> ChartSpec:
    """Pydantic + real-column validation. Raises ValueError with a fixable message."""
    try:
        spec = ChartSpec.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:4])) from exc
    known = set(columns)
    for field in ("x", "y", "series"):
        col = getattr(spec, field)
        if col is not None and col not in known:
            raise ValueError(f"{field}='{col}' is not a column in this sheet. "
                             f"Valid columns: {', '.join(columns[:40])}")
    for f in spec.filters:
        if f.column not in known:
            raise ValueError(f"filter column '{f.column}' is not in this sheet")
    return spec


# --------------------------------------------------------------------------- #
# schema summary
# --------------------------------------------------------------------------- #
def schema_summary(df: pd.DataFrame, types: dict[str, str], sheet: str,
                   max_cols: int = 40) -> dict:
    cols = []
    for c in list(df.columns)[:max_cols]:
        s = df[c]
        entry: dict[str, Any] = {
            "name": str(c),
            "type": types.get(c, "text"),
            "cardinality": int(s.nunique(dropna=True)),
            "samples": [profiling._safe(v) for v in s.dropna().head(3).tolist()],
        }
        if types.get(c) == profiling.NUM:
            sn = pd.to_numeric(s, errors="coerce").dropna()
            if len(sn):
                entry["min"], entry["max"] = profiling._safe(sn.min()), profiling._safe(sn.max())
        cols.append(entry)
    return {"sheet": sheet, "rows": int(len(df)), "columns": cols}


def suggestions(df: pd.DataFrame, types: dict[str, str], limit: int = 4) -> list[str]:
    """Chips shown in the command bar, derived from the dataset."""
    cats = [c for c, t in types.items() if t in (profiling.CAT, profiling.BOOL)]
    nums = [c for c, t in types.items() if t == profiling.NUM]
    dates = [c for c, t in types.items() if t == profiling.DATE]
    out: list[str] = []
    if nums and cats:
        out.append(f"show {nums[0]} by {cats[0]} as a treemap")
        out.append(f"total {nums[0]} per {cats[-1]} as a horizontal bar")
    if nums and dates:
        out.append(f"{nums[0]} over {dates[0]} as a line")
    if len(nums) >= 2:
        out.append(f"{nums[0]} vs {nums[1]} as a scatter")
    if cats:
        out.append(f"count of records by {cats[0]} as a pie")
    if nums:
        out.append(f"distribution of {nums[0]} as a histogram")
    return out[:limit]


# --------------------------------------------------------------------------- #
# keyword fallback (no LLM)
# --------------------------------------------------------------------------- #
KEYWORDS: list[tuple[str, str]] = [
    ("treemap", "treemap"), ("sunburst", "sunburst"), ("waterfall", "waterfall"),
    ("funnel", "funnel"), ("sankey", "sankey"), ("pareto", "pareto"),
    ("candlestick", "candlestick"), ("word cloud", "wordcloud"), ("wordcloud", "wordcloud"),
    ("calendar", "calendar"), ("heatmap", "heatmap"), ("heat map", "heatmap"),
    ("correlation", "heatmap"), ("histogram", "histogram"), ("distribution", "histogram"),
    ("density", "kde"), ("box", "box"), ("violin", "violin"), ("radar", "radar"),
    ("polar", "polar_area"), ("radial", "radial_bar"), ("gauge", "gauge"),
    ("donut", "donut"), ("doughnut", "donut"), ("pie", "pie"), ("share", "pie"),
    ("bubble", "bubble"), ("regression", "regression"), ("trendline", "regression"),
    ("scatter", "scatter"), ("vs", "scatter"), ("parallel", "parallel"),
    ("3d scatter", "scatter3d"), ("surface", "surface3d"), ("3d bar", "bar3d"),
    ("kpi", "kpi"), ("sparkline", "kpi"),
    ("stacked area", "stacked_area"), ("area", "area"), ("step", "step_line"),
    ("multi line", "multi_line"), ("multi-line", "multi_line"),
    ("line", "line"), ("trend", "line"), ("over time", "line"),
    ("stacked bar", "stacked_bar"), ("grouped bar", "grouped_bar"),
    ("horizontal bar", "horizontal_bar"), ("ranking", "horizontal_bar"),
    ("bar", "bar"), ("column", "bar"), ("compare", "bar"),
]

AGG_WORDS = [("average", "mean"), ("avg", "mean"), ("mean", "mean"), ("median", "median"),
             ("count", "count"), ("number of", "count"), ("how many", "count"),
             ("max", "max"), ("highest", "max"), ("min", "min"), ("lowest", "min"),
             ("total", "sum"), ("sum", "sum")]


def _match_columns(prompt: str, columns: list[str]) -> list[str]:
    low = prompt.lower()
    hits = []
    for c in columns:
        cl = str(c).lower()
        if cl and cl in low:
            hits.append((low.index(cl), c))
        else:
            words = [w for w in re.split(r"[^a-z0-9]+", cl) if len(w) > 3]
            if words and all(w in low for w in words):
                hits.append((min(low.index(w) for w in words), c))
    hits.sort()
    return [c for _i, c in hits]


def keyword_spec(prompt: str, df: pd.DataFrame, types: dict[str, str]) -> dict:
    low = (prompt or "").lower()
    ctype = next((t for kw, t in KEYWORDS if kw in low), None)
    agg = next((a for kw, a in AGG_WORDS if kw in low), None)
    mentioned = _match_columns(low, list(df.columns))
    nums = [c for c in mentioned if types.get(c) == profiling.NUM]
    cats = [c for c in mentioned if types.get(c) in (profiling.CAT, profiling.BOOL,
                                                     profiling.TEXT)]
    dates = [c for c in mentioned if types.get(c) == profiling.DATE]
    all_nums = [c for c, t in types.items() if t == profiling.NUM]
    all_cats = [c for c, t in types.items() if t in (profiling.CAT, profiling.BOOL)]
    all_dates = [c for c, t in types.items() if t == profiling.DATE]

    x = (dates or cats or all_dates or all_cats or list(df.columns))[0] if len(df.columns) else None
    y = (nums or all_nums or [None])[0]
    if ctype is None:
        ctype = "line" if (dates or (not cats and all_dates)) else ("bar" if y else "wordcloud")
    if ctype in ("line", "area", "step_line", "multi_line", "stacked_area", "calendar",
                 "candlestick"):
        x = (dates or all_dates or [x])[0]
    if ctype in ("scatter", "bubble", "regression", "scatter3d", "surface3d"):
        pool = nums + [c for c in all_nums if c not in nums]
        if len(pool) >= 2:
            x, y = pool[0], pool[1]
    if ctype in ("histogram", "kde"):
        x = (nums or all_nums or [x])[0]
        y = None
    if ctype in ("heatmap", "parallel", "kpi"):
        x, y = None, None
    if ctype == "wordcloud":
        x = next((c for c, t in types.items() if t in (profiling.TEXT, profiling.CAT)), x)
        y = None
    spec: dict[str, Any] = {"type": ctype}
    if x:
        spec["x"] = x
    if y and ctype not in ("wordcloud", "histogram", "kde", "heatmap", "parallel", "kpi"):
        spec["y"] = y
    if spec.get("y") and ctype not in ("scatter", "bubble", "regression", "scatter3d"):
        spec["agg"] = agg or "sum"
    if ctype in ("grouped_bar", "stacked_bar", "multi_line", "stacked_area", "sankey"):
        extra = next((c for c in all_cats if c != spec.get("x")), None)
        if extra:
            spec["series"] = extra
    return spec


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #
SYSTEM = """You translate a data question into ONE chart specification.
Reply with a single JSON object and nothing else — no prose, no markdown fence.

Schema:
{"type": <one of the supported chart types>, "x": <column|null>, "y": <column|null>,
 "series": <column|null>, "agg": "sum"|"mean"|"median"|"count"|"min"|"max"|null,
 "title": <short string|null>, "filters": [{"column": <column>, "op": "in"|"contains"|">"|">="|"<"|"<="|"==", "value": <any>}]}

Rules:
- x, y, series and filter columns MUST be exact column names from the provided schema.
- Pick the chart type that best answers the question; honour an explicitly requested type.
- Numeric measures belong in y (except histogram/kde/scatter families where x is numeric).
- Use agg=count when the question counts records.
"""


def llm_enabled() -> bool:
    return bool(settings.nl_charts_enabled and os.environ.get("ANTHROPIC_API_KEY"))


def _call_anthropic(messages: list[dict]) -> str:
    from anthropic import Anthropic
    client = Anthropic()
    msg = client.messages.create(model=settings.nl_model, max_tokens=700, system=SYSTEM,
                                 messages=messages)
    parts = []
    for block in msg.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("model did not return a JSON object")
    return json.loads(text[start:end + 1])


async def propose_spec(prompt: str, df: pd.DataFrame, types: dict[str, str], sheet: str,
                       call=None) -> tuple[dict, str, str | None]:
    """Returns (spec_dict, source, warning). source in {llm, llm_retry, keyword}."""
    columns = [str(c) for c in df.columns]
    if not llm_enabled() and call is None:
        return keyword_spec(prompt, df, types), "keyword", None

    caller = call or (lambda msgs: asyncio.to_thread(_call_anthropic, msgs))
    schema = json.dumps(schema_summary(df, types, sheet), default=str)
    messages = [{"role": "user",
                 "content": f"Schema:\n{schema}\n\nSupported chart types: "
                            f"{', '.join(CHART_TYPES)}\n\nQuestion: {prompt}"}]
    last_err: str | None = None
    raw: str | None = None
    for attempt in (1, 2):
        try:
            raw = await caller(messages)
            payload = extract_json(raw if isinstance(raw, str) else str(raw))
            spec = validate_spec(payload, columns)
            return spec.model_dump(exclude_none=True), ("llm" if attempt == 1 else "llm_retry"), None
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            if attempt == 1:
                messages = messages + [
                    {"role": "assistant", "content": raw if isinstance(raw, str) and raw else "{}"},
                    {"role": "user",
                     "content": f"That was invalid: {last_err}. Reply with corrected JSON only."},
                ]
    return keyword_spec(prompt, df, types), "keyword", f"Model output rejected ({last_err})."


def build_slide(spec: dict, df: pd.DataFrame, types: dict[str, str], sheet: str,
                prompt: str) -> dict:
    """Build the chart payload; raises charting.ChartError when impossible."""
    spec = {k: v for k, v in spec.items() if v not in (None, [], "")}
    filters = spec.pop("filters", [])
    frame = df
    for f in filters:
        col, op, val = f.get("column"), f.get("op"), f.get("value")
        if col not in frame.columns:
            continue
        try:
            if op == "in" and isinstance(val, list):
                frame = frame[frame[col].astype(str).isin([str(v) for v in val])]
            elif op == "contains":
                frame = frame[frame[col].astype(str).str.contains(str(val), case=False, na=False)]
            elif op in (">", ">=", "<", "<=", "==") and val is not None:
                s = pd.to_numeric(frame[col], errors="coerce")
                v = float(val)
                mask = {">": s > v, ">=": s >= v, "<": s < v, "<=": s <= v, "==": s == v}[op]
                frame = frame[mask.fillna(False)]
        except Exception:  # noqa: BLE001
            continue
    if frame.empty:
        raise charting.ChartError("No rows match those filters.")
    title = spec.pop("title", None)
    data = charting.build(frame, types, dict(spec))
    meta = charting.CHART_META.get(spec["type"], (spec["type"], ""))
    return {
        "id": f"nl-{abs(hash((prompt, json.dumps(spec, sort_keys=True, default=str)))) % 10**8}",
        "type": spec["type"], "name": meta[0], "rationale": meta[1],
        "title": title or planner.title_for(spec, sheet),
        "insight": planner.insight(spec, data), "spec": spec, "data": data,
        "prompt": prompt, "rows": int(len(frame)),
    }
