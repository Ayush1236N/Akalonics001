"""Demand Forecasting module — AkaLonics.

Independent, testable forecasting engine used by the "Demand Forecasting" feature.
Given a category's historical demand records it aggregates to a chosen time
granularity and runs all seven forecasting models side by side:

    SES · Holt · Holt-Winters · Auto-ARIMA (lite) · Croston's · Linear Regression · Gradient Boosting (lite)

Design notes
------------
* No new heavy dependencies were introduced (no statsmodels / scikit-learn / xgboost) —
  everything here is implemented on top of numpy + pandas, which the project already
  depends on. See the README for the trade-offs that implies.
* Category only ever changes *which rows* are selected — every model function is
  category-agnostic and operates on a plain 1-D numpy array of period totals.
* Every model returns a common shape: `(fitted, forecast, meta)` so new models can be
  added by writing one function and adding it to `MODELS` below.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# categories / granularities
# --------------------------------------------------------------------------- #
CATEGORIES: dict[str, str] = {
    "T7-F": "Finished Goods",
    "T7-SFG": "Semi-Finished Goods",
    "T7-R": "Raw Material",
}

GRANULARITIES = ["daily", "weekly", "monthly", "quarterly", "yearly"]

_FREQ = {"daily": "D", "weekly": "W-MON", "monthly": "MS", "quarterly": "QS", "yearly": "YS"}
# forecast horizon (# of future periods) shown per granularity
HORIZON = {"daily": 14, "weekly": 8, "monthly": 6, "quarterly": 4, "yearly": 3}
# seasonal period length (in periods) used by Holt-Winters / regression seasonal dummies
SEASON_M = {"daily": 7, "weekly": 4, "monthly": 12, "quarterly": 4, "yearly": 1}


def label_for(ts: pd.Timestamp, granularity: str) -> str:
    ts = pd.Timestamp(ts)
    if granularity == "daily":
        return ts.strftime("%d %b")
    if granularity == "weekly":
        return f"Wk{ts.isocalendar().week:02d} '{str(ts.year)[2:]}"
    if granularity == "monthly":
        return ts.strftime("%b %Y")
    if granularity == "quarterly":
        return f"Q{(ts.month - 1) // 3 + 1} {ts.year}"
    return str(ts.year)


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #
def aggregate(df: pd.DataFrame, granularity: str, sku: str | None = None) -> pd.Series:
    """Aggregate raw {date, sku, quantity} rows into a period-indexed total series.

    sku=None or "__all__" rolls the whole category up into one series.
    """
    d = df if sku in (None, "__all__") else df[df["sku"] == sku]
    if d.empty:
        return pd.Series(dtype=float)
    s = d.set_index("date")["quantity"].resample(_FREQ[granularity]).sum()
    if s.empty:
        return s
    s = s.asfreq(_FREQ[granularity], fill_value=0.0)
    return s.astype(float)


# --------------------------------------------------------------------------- #
# demand-pattern classification → routing recommendation
# --------------------------------------------------------------------------- #
def classify_pattern(y: np.ndarray, granularity: str) -> tuple[str, str]:
    """Return (pattern_name, recommended_model_id). All 7 models still run regardless."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n == 0:
        return "unknown", "ses"

    zero_frac = float(np.mean(y == 0))
    if zero_frac >= 0.3 and n >= 6:
        return "intermittent", "croston"

    m = SEASON_M.get(granularity, 1)
    if m > 1 and n >= 2 * m:
        idx = np.arange(n) % m
        season_means = np.array([y[idx == s].mean() for s in range(m)])
        season_var = float(np.var(season_means))
        total_var = float(np.var(y)) + 1e-9
        if season_var / total_var > 0.15:
            return "seasonal", "holt_winters"

    if n >= 3:
        slope = float(np.polyfit(np.arange(n), y, 1)[0])
        if abs(slope) > 0.05 * (np.mean(y) + 1e-9):
            return "trending", "holt"

    if n >= 24:
        return "high_volume", "gboost"

    return "stable", "ses"


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def compute_metrics(actual: np.ndarray, fitted: np.ndarray) -> dict[str, float | None]:
    actual = np.asarray(actual, dtype=float)
    fitted = np.asarray(fitted, dtype=float)
    err = actual - fitted
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err**2)))
    denom = np.where(actual == 0, np.nan, actual)
    with np.errstate(invalid="ignore", divide="ignore"):
        mape_arr = np.abs(err / denom) * 100
    mape = float(np.nanmean(mape_arr)) if np.any(~np.isnan(mape_arr)) else None
    return {
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "mape": None if mape is None or not math.isfinite(mape) else round(mape, 2),
    }


# --------------------------------------------------------------------------- #
# model 1 — Simple Exponential Smoothing
# --------------------------------------------------------------------------- #
def m_ses(y: np.ndarray, h: int, alpha: float = 0.3):
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n == 0:
        return np.zeros(0), np.zeros(h), {"alpha": alpha}
    level = y[0]
    fitted = np.empty(n)
    fitted[0] = level
    for t in range(1, n):
        fitted[t] = level
        level = alpha * y[t] + (1 - alpha) * level
    forecast = np.full(h, level)
    return fitted, forecast, {"alpha": alpha}


# --------------------------------------------------------------------------- #
# model 2 — Holt's Linear Trend
# --------------------------------------------------------------------------- #
def m_holt(y: np.ndarray, h: int, alpha: float = 0.4, beta: float = 0.2):
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n == 0:
        return np.zeros(0), np.zeros(h), {"alpha": alpha, "beta": beta}
    if n == 1:
        return np.array([y[0]]), np.full(h, y[0]), {"alpha": alpha, "beta": beta}
    level = np.zeros(n)
    trend = np.zeros(n)
    fitted = np.zeros(n)
    level[0] = y[0]
    trend[0] = y[1] - y[0]
    fitted[0] = y[0]
    for t in range(1, n):
        fitted[t] = level[t - 1] + trend[t - 1]
        level[t] = alpha * y[t] + (1 - alpha) * (level[t - 1] + trend[t - 1])
        trend[t] = beta * (level[t] - level[t - 1]) + (1 - beta) * trend[t - 1]
    forecast = np.array([level[-1] + (i + 1) * trend[-1] for i in range(h)])
    return fitted, forecast, {"alpha": alpha, "beta": beta}


# --------------------------------------------------------------------------- #
# model 3 — Holt-Winters additive seasonal (falls back to Holt if too little history)
# --------------------------------------------------------------------------- #
def m_holt_winters(y: np.ndarray, h: int, granularity: str,
                    alpha: float = 0.3, beta: float = 0.15, gamma: float = 0.25):
    y = np.asarray(y, dtype=float)
    n = len(y)
    m = SEASON_M.get(granularity, 1)
    if m <= 1 or n < 2 * m:
        fitted, forecast, meta = m_holt(y, h, alpha=0.4, beta=0.2)
        meta = {**meta, "seasonal_period": None, "fallback": "holt (insufficient history for seasonality)"}
        return fitted, forecast, meta

    level = np.zeros(n)
    trend = np.zeros(n)
    season = np.zeros(n)
    fitted = np.zeros(n)

    # initial seasonal indices from the first two full cycles
    season_init = y[:2 * m].reshape(2, m).mean(axis=0)
    season_init = season_init - season_init.mean()
    season[:m] = season_init
    level[m - 1] = y[:m].mean()
    trend[m - 1] = (y[m:2 * m].mean() - y[:m].mean()) / m
    fitted[:m] = y[:m]

    for t in range(m, n):
        s_prev = season[t - m]
        fitted[t] = level[t - 1] + trend[t - 1] + s_prev
        level[t] = alpha * (y[t] - s_prev) + (1 - alpha) * (level[t - 1] + trend[t - 1])
        trend[t] = beta * (level[t] - level[t - 1]) + (1 - beta) * trend[t - 1]
        season[t] = gamma * (y[t] - level[t]) + (1 - gamma) * s_prev

    forecast = np.array([
        level[-1] + (i + 1) * trend[-1] + season[n - m + (i % m)]
        for i in range(h)
    ])
    return fitted, forecast, {"alpha": alpha, "beta": beta, "gamma": gamma, "seasonal_period": m}


# --------------------------------------------------------------------------- #
# model 4 — Auto-ARIMA (lite grid search over small p/d, OLS-based)
# --------------------------------------------------------------------------- #
def _fit_ar(yd: np.ndarray, p: int):
    n = len(yd)
    if n <= p + 2:
        return None
    X = np.column_stack([yd[p - 1 - i: n - 1 - i] for i in range(p)])
    Y = yd[p:]
    X1 = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X1, Y, rcond=None)
    resid = Y - X1 @ beta
    rss = float(np.sum(resid**2))
    k = p + 1
    n_eff = len(Y)
    aicc = n_eff * math.log(rss / n_eff + 1e-9) + 2 * k + (2 * k * (k + 1)) / max(n_eff - k - 1, 1)
    return beta, aicc, X1, n_eff


def m_auto_arima(y: np.ndarray, h: int):
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n < 8:
        fitted, forecast, meta = m_holt(y, h)
        return fitted, forecast, {**meta, "order": None, "fallback": "holt (series too short)"}

    best = None  # (aicc, d, p, beta)
    for d in (0, 1):
        yd = np.diff(y, n=d) if d else y.copy()
        if len(yd) < 6:
            continue
        for p in (1, 2, 3):
            fit = _fit_ar(yd, p)
            if fit is None:
                continue
            beta, aicc, _X1, _ = fit
            if best is None or aicc < best[0]:
                best = (aicc, d, p, beta)

    if best is None:
        fitted, forecast, meta = m_holt(y, h)
        return fitted, forecast, {**meta, "order": None, "fallback": "holt (no ARIMA order converged)"}

    _, d, p, beta = best
    yd = np.diff(y, n=d) if d else y.copy()
    nfit = len(yd)
    X = np.column_stack([yd[p - 1 - i: nfit - 1 - i] for i in range(p)])
    X1 = np.column_stack([np.ones(len(X)), X])
    fitted_d = X1 @ beta  # aligned to yd[p:]

    # forecast recursively on the differenced scale
    hist = list(yd[-p:])
    fc_d = []
    for _ in range(h):
        val = beta[0] + sum(beta[i + 1] * hist[-1 - i] for i in range(p))
        fc_d.append(val)
        hist.append(val)
    fc_d = np.array(fc_d)

    if d == 0:
        fitted = np.concatenate([y[:p], fitted_d])
        forecast = fc_d
    else:  # d == 1, integrate back once
        fitted = y.copy()
        fitted[p + 1:] = y[p: n - 1] + fitted_d
        forecast = y[-1] + np.cumsum(fc_d)

    return fitted, forecast, {"order": (p, d, 0), "fallback": None}


# --------------------------------------------------------------------------- #
# model 5 — Croston's Method (intermittent demand)
# --------------------------------------------------------------------------- #
def m_croston(y: np.ndarray, h: int, alpha: float = 0.2):
    y = np.asarray(y, dtype=float)
    n = len(y)
    fitted = np.zeros(n)
    if n == 0 or not np.any(y > 0):
        return fitted, np.zeros(h), {"alpha": alpha, "note": "no non-zero demand observed"}

    first_nz = int(np.argmax(y > 0))
    z = y[first_nz]
    p = 1.0
    q = 1
    rate = z / p
    fitted[: first_nz + 1] = rate
    for t in range(first_nz + 1, n):
        if y[t] > 0:
            z = alpha * y[t] + (1 - alpha) * z
            p = alpha * q + (1 - alpha) * p
            q = 1
        else:
            q += 1
        rate = z / p if p > 0 else 0.0
        fitted[t] = rate

    forecast = np.full(h, rate)
    return fitted, forecast, {"alpha": alpha, "avg_demand_size": round(float(z), 3),
                              "avg_interval": round(float(p), 3)}


# --------------------------------------------------------------------------- #
# model 6 — Multiple Linear Regression (trend + seasonal dummies; pluggable exogenous)
# --------------------------------------------------------------------------- #
def m_linreg(y: np.ndarray, h: int, granularity: str, exog: np.ndarray | None = None,
             exog_future: np.ndarray | None = None):
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n == 0:
        return np.zeros(0), np.zeros(h), {"seasonal_period": None, "exogenous": False}
    t = np.arange(n)
    m = SEASON_M.get(granularity, 1)
    use_season = m > 1 and n >= 2 * m
    cols = [np.ones(n), t.astype(float)]
    if use_season:
        cols += [(t % m == s).astype(float) for s in range(1, m)]
    if exog is not None and len(exog) == n:
        cols.append(np.asarray(exog, dtype=float))
    X = np.column_stack(cols)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    fitted = X @ beta

    tf = np.arange(n, n + h).astype(float)
    fcols = [np.ones(h), tf]
    if use_season:
        fcols += [((tf.astype(int) % m) == s).astype(float) for s in range(1, m)]
    if exog is not None and exog_future is not None and len(exog_future) == h:
        fcols.append(np.asarray(exog_future, dtype=float))
    elif exog is not None:
        fcols.append(np.full(h, float(np.mean(exog))))
    Xf = np.column_stack(fcols)
    forecast = Xf @ beta
    return fitted, forecast, {"seasonal_period": m if use_season else None, "exogenous": exog is not None}


# --------------------------------------------------------------------------- #
# model 7 — Gradient Boosting (lite): boosted regression stumps on lag/time features
# --------------------------------------------------------------------------- #
def m_gboost(y: np.ndarray, h: int, n_estimators: int = 40, lr: float = 0.15, max_lag: int = 3):
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n <= max_lag + 3:
        fitted, forecast, meta = m_ses(y, h)
        return fitted, forecast, {**meta, "fallback": "ses (series too short for lag features)"}

    X = np.column_stack(
        [y[max_lag - i - 1: n - i - 1] for i in range(max_lag)] + [np.arange(max_lag, n).astype(float)]
    )
    Y = y[max_lag:]
    base = float(Y.mean())
    resid = Y - base
    stumps: list[tuple[int, float, float, float]] = []

    for _ in range(n_estimators):
        best = None
        for f in range(X.shape[1]):
            xs = X[:, f]
            uniq = np.unique(xs)
            if len(uniq) < 2:
                continue
            thresholds = (uniq[:-1] + uniq[1:]) / 2
            for thr in thresholds:
                left = xs <= thr
                right = ~left
                if left.sum() == 0 or right.sum() == 0:
                    continue
                lval = float(resid[left].mean())
                rval = float(resid[right].mean())
                sse = float(np.sum((resid[left] - lval) ** 2) + np.sum((resid[right] - rval) ** 2))
                if best is None or sse < best[0]:
                    best = (sse, f, float(thr), lval, rval)
        if best is None:
            break
        _, f, thr, lval, rval = best
        stumps.append((f, thr, lval, rval))
        update = np.where(X[:, f] <= thr, lval, rval)
        resid = resid - lr * update

    def predict_row(feats: np.ndarray) -> float:
        val = base
        for f, thr, lval, rval in stumps:
            val += lr * (lval if feats[f] <= thr else rval)
        return val

    fitted_core = np.array([predict_row(X[i]) for i in range(len(X))])
    fitted = np.concatenate([np.full(max_lag, y[:max_lag].mean()), fitted_core])

    hist = list(y)
    forecast = []
    for _ in range(h):
        feats = np.array([hist[-1 - i] for i in range(max_lag)] + [float(len(hist))])
        val = predict_row(feats)
        forecast.append(val)
        hist.append(val)

    return fitted, np.array(forecast), {"n_estimators": len(stumps), "learning_rate": lr, "max_lag": max_lag}


# --------------------------------------------------------------------------- #
# model registry
# --------------------------------------------------------------------------- #
MODEL_NAMES = {
    "ses": "Simple Exponential Smoothing",
    "holt": "Holt's Linear Trend",
    "holt_winters": "Holt-Winters Seasonal",
    "auto_arima": "Auto-ARIMA",
    "croston": "Croston's Method",
    "linreg": "Multiple Linear Regression",
    "gboost": "Gradient Boosting",
}
MODEL_BEST_FOR = {
    "ses": "Flat / stable demand, no trend or seasonality",
    "holt": "Clear upward or downward trend",
    "holt_winters": "Seasonal products",
    "auto_arima": "Complex autocorrelated, stationary / differenced series",
    "croston": "Intermittent / sparse demand (e.g. spare parts)",
    "linreg": "Demand driven by external / exogenous factors",
    "gboost": "High-volume data with rich feature sets",
}
MODEL_ORDER = ["ses", "holt", "holt_winters", "auto_arima", "croston", "linreg", "gboost"]


def _run_one(model_id: str, y: np.ndarray, h: int, granularity: str):
    if model_id == "ses":
        return m_ses(y, h)
    if model_id == "holt":
        return m_holt(y, h)
    if model_id == "holt_winters":
        return m_holt_winters(y, h, granularity)
    if model_id == "auto_arima":
        return m_auto_arima(y, h)
    if model_id == "croston":
        return m_croston(y, h)
    if model_id == "linreg":
        return m_linreg(y, h, granularity)
    if model_id == "gboost":
        return m_gboost(y, h)
    raise ValueError(f"Unknown model id: {model_id}")


@dataclass
class ModelResult:
    model: str
    name: str
    best_for: str
    fitted: np.ndarray
    forecast: np.ndarray
    metrics: dict
    is_recommended: bool
    meta: dict = field(default_factory=dict)


def run_all_models(series: pd.Series, granularity: str) -> tuple[list[ModelResult], str, str]:
    """Run all seven models against one aggregated series. Returns (results, pattern, recommended_id)."""
    y = series.to_numpy(dtype=float)
    h = HORIZON.get(granularity, 6)
    pattern, recommended = classify_pattern(y, granularity)
    if len(y) < 2:
        recommended = "ses"

    results: list[ModelResult] = []
    for model_id in MODEL_ORDER:
        try:
            fitted, forecast, meta = _run_one(model_id, y, h, granularity)
        except Exception as exc:  # noqa: BLE001 — never let one model crash the comparison
            fitted, forecast, meta = np.zeros_like(y), np.zeros(h), {"error": str(exc)}
        metrics = compute_metrics(y, fitted) if len(y) else {"mae": None, "rmse": None, "mape": None}
        results.append(ModelResult(
            model=model_id, name=MODEL_NAMES[model_id], best_for=MODEL_BEST_FOR[model_id],
            fitted=fitted, forecast=forecast, metrics=metrics,
            is_recommended=(model_id == recommended), meta=meta,
        ))
    return results, pattern, recommended
