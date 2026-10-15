# Demand Forecasting

A feature module added on top of AkaLonics' existing chart studio. It lets a demand-planning
user compare **seven forecasting models** side by side, for **three inventory categories**, at
**five time granularities** — using demand history they type in (or a bundled sample dataset).

Open it from the **"Demand Forecasting"** button in the top bar — it doesn't require an uploaded
dataset like the rest of the app.

## 1. Navigation

```
Demand Forecasting
├── T7-F    Finished Goods
├── T7-SFG  Semi-Finished Goods
└── T7-R    Raw Material
```

All three categories run through the exact same component and the exact same backend code path —
category is just a filter on which rows get queried (`forecasting.py` has no category-specific
branches). Inside a category you can drill down from **the category total → an individual SKU**,
and switch granularity (Daily / Weekly / Monthly / Quarterly / Yearly) at any time; every switch
re-aggregates the raw history and re-runs all seven models — nothing is pre-baked.

## 2. Getting data in

Click **"Manage demand data"** to:
- **Load synthetic sample** — generates ~2.5 years of realistic daily history per category, with
  SKUs deliberately covering stable, trending, seasonal and intermittent patterns so all seven
  models and the routing logic have something meaningful to do.
- **Load a bundled real dataset** — two real datasets are shipped with the app (converted from the
  project's original Excel sources) and selectable per category from the same panel:
  `dummy_1000` (1,000 rows of daily Date/Product ID/Actual demand across several SKUs) and
  `test2_shirt` (daily July + August production history for a single SKU). Both are listed by
  `GET /api/forecast/meta` under `bundled` and loaded via `POST /api/forecast/bundled/{name}`.
- **Enter rows manually** — Date, SKU / item identifier, Quantity.

Loading a sample or bundled dataset **replaces** whatever history is currently loaded for that
category (for that user); manual entry **appends** by default.

Data is stored server-side in Postgres/SQLite (`ForecastRow`, see `models.py`), scoped to the
signed-in user exactly like every other resource in the app — it is **not** a file cache, so it
survives restarts and redeploys and is never visible to another account.

## 3. The seven models

| # | Model | Best suited for |
|---|-------|------------------|
| 1 | Simple Exponential Smoothing (SES) | Flat / stable demand |
| 2 | Holt's Linear Trend | Clear upward/downward trend |
| 3 | Holt-Winters Seasonal | Seasonal products |
| 4 | Auto-ARIMA (lite) | Autocorrelated / differenced series |
| 5 | Croston's Method | Intermittent / sparse demand |
| 6 | Multiple Linear Regression | Trend + (future) exogenous drivers |
| 7 | Gradient Boosting (lite) | High-volume data, rich feature sets |

All seven always run and are always shown — the backend also classifies the series' demand
pattern (`% zero periods`, seasonal-variance ratio, trend slope, history length) and flags one
model as **Recommended**, but that's a badge, not a filter.

**Implementation note:** no new heavy dependency was added (no statsmodels / scikit-learn /
xgboost) — everything is written directly on top of numpy + pandas, which the project already
depends on. Auto-ARIMA is a small OLS-based grid search over `p ∈ {1,2,3}`, `d ∈ {0,1}` rather
than a full stepwise search, and Gradient Boosting is a from-scratch boosted-stump ensemble over
lag/time features rather than XGBoost/LightGBM. Both are real, working implementations — just
lighter-weight than the named libraries, so behaviour on very large or heavily seasonal (weekly,
`m=52`) datasets will be less refined than a statsmodels/xgboost-backed version. Swapping in those
libraries later only touches `forecasting.py` (see "Adding a new model" below) — nothing else in
the stack needs to change.

## 4. Chart views (per model)

Every model gets the same **4-view panel**, switchable via tabs:

1. **Line** — actual history + that model's fitted line + its forecast, over the selected
   granularity. Default view.
2. **Bar** — grouped actual-vs-forecast bars, period over period.
3. **Pie** — composition, not trend: share of the *forecast horizon* by period.
4. **3D surface** (shown once per screen, not once per model, to avoid rendering the same 7
   forecasts as 7 near-identical surfaces):
   - At the **category** level: `Time × SKU × Demand` — every SKU's history plus its
     recommended-model forecast, so builds/drop-offs across items are visible together.
   - At the **single-SKU drill-down**: `Time × Model × Demand` ribbon — all seven models'
     forecasts for that one SKU, layered so their shapes can be compared directly.

Statistical detail (α/β/γ, ARIMA order, Croston's demand-size/interval, boosting learning rate)
lives behind a per-model **"Show advanced parameters"** toggle, not the default view.

The three chart types reuse the project's existing Chart.js dispatcher (`charts/index.jsx`,
`multi_line` / `grouped_bar` / `pie`) and the existing Three.js surface renderer
(`charts/three3d.jsx`, `surface3d`) — no new charting library was introduced.

## 4a. Future Demand table

Below the metrics table sits a **Future Demand** table: every upcoming period (the same horizon
used by the charts — 14 days / 8 weeks / 6 months / 4 quarters / 3 years depending on
granularity) as rows, all seven models' predicted quantity as columns, with the Recommended
model's column called out. Unlike the chart panel — which shows one model at a time — this table
always shows all seven side by side, for whatever is currently drilled into (category total or a
single SKU), and exports to CSV. It reads directly from each model's `future` array in the
`/api/forecast/run` response (see the schema below) — no extra API call.

## 5. Data schema

**Historical input row** (what you type in, or what a POST to `/api/forecast/data` expects):

```json
{ "date": "2026-08-24", "sku": "FG-JACKET-01", "quantity": 12 }
```

**Model result** (internal shape returned by `forecasting.run_all_models`, and what each entry of
the `/api/forecast/run` response's `models[]` array is built from — `future` is the plain array
the Future Demand table renders):

```json
{
  "model": "holt_winters",
  "name": "Holt-Winters Seasonal",
  "forecast": [/* future period values */],
  "future": [/* same values, rounded — what the Future Demand table reads */],
  "metrics": { "mape": 8.4, "rmse": 5.1, "mae": 3.9 },
  "is_recommended": true
}
```

## 6. API

```
GET    /api/forecast/meta                 categories, granularities, model list, bundled datasets
GET    /api/forecast/data/{category}      current rows + SKUs for a category
POST   /api/forecast/data                 { category, mode: "append"|"replace", rows:[{date,sku,quantity}] }
DELETE /api/forecast/data/{category}      clears that category's demand history
POST   /api/forecast/sample/{category}    loads synthetic demo history (replaces existing)
POST   /api/forecast/bundled/{name}       { category } → loads a bundled real dataset (replaces existing)
POST   /api/forecast/run                  { category, granularity, sku } → forecasts + ready-to-render charts + future_labels
```

All of the above require the same bearer-token session as the rest of the app; data is scoped to
the signed-in user. `sku` in the run request is either a SKU id or `"__all__"` for the category
rollup.

## 7. Extending it

**Add a new model** — write one function in `backend/forecasting.py` with the signature
`(y: np.ndarray, h: int, ...) -> (fitted, forecast, meta)`, add it to `MODEL_NAMES`,
`MODEL_BEST_FOR`, `MODEL_ORDER`, and one `if model_id == "...":` branch in `_run_one`. That's it —
aggregation, metrics, charting and the UI all pick it up automatically because they iterate
`MODEL_ORDER`.

**Add a new category** — add one entry to `CATEGORIES` in `forecasting.py` (id → display name).
No other code path is category-specific; the frontend tabs are generated from
`GET /api/forecast/meta`.

**Add a new chart type to the per-model panel** — add an entry to the `TABS` array and a branch in
the `slide` `useMemo` inside `frontend/src/views/DemandForecasting.jsx`'s `ModelChartPanel`, using
any chart type already supported by `charts/index.jsx` (or a new one added there first).

**Phase 2 (already accounted for in the design, not built yet):** `m_linreg` already accepts
`exog` / `exog_future` arrays for pluggable external regressors (price, promotions, weather) —
Phase 2 only needs to add a UI for supplying those columns and pass them through; no forecasting
code needs to change.
