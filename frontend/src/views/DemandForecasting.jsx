import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api';
import { EmptyState, ErrorState, Icon, Spinner, Toggle } from '../components/ui';
import ChartView from '../charts/index.jsx';
import { fmtNum } from '../lib/format';

const GRAN_LABELS = { daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly', quarterly: 'Quarterly', yearly: 'Yearly' };

export default function DemandForecasting() {
  const [meta, setMeta] = useState(null);
  const [metaErr, setMetaErr] = useState(null);
  const [category, setCategory] = useState('T7-F');
  const [granularity, setGranularity] = useState('monthly');
  const [sku, setSku] = useState('__all__');
  const [skus, setSkus] = useState([]);
  const [rows, setRows] = useState([]);
  const [rowCount, setRowCount] = useState(0);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [showEntry, setShowEntry] = useState(false);
  const [activeModel, setActiveModel] = useState(null);
  const [chartTab, setChartTab] = useState('line');
  const [advanced, setAdvanced] = useState(false);
  const [draft, setDraft] = useState([{ date: '', sku: '', quantity: '' }]);
  const [draftErr, setDraftErr] = useState(null);
  const [draftBusy, setDraftBusy] = useState(false);

  useEffect(() => {
    api.forecastMeta().then(setMeta).catch((e) => setMetaErr(e.message));
  }, []);

  const loadData = useCallback((cat) => {
    api
      .forecastData(cat)
      .then((r) => {
        setSkus(r.skus);
        setRows(r.rows);
        setRowCount(r.count);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    setSku('__all__');
    setResult(null);
    setError(null);
    setActiveModel(null);
    loadData(category);
  }, [category, loadData]);

  const runForecast = useCallback(() => {
    setBusy(true);
    setError(null);
    api
      .forecastRun({ category, granularity, sku })
      .then((r) => {
        setResult(r);
        setActiveModel((prev) => r.models.find((m) => m.model === prev)?.model || r.recommended);
      })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  }, [category, granularity, sku]);

  useEffect(() => {
    if (rowCount > 0) runForecast();
    else setResult(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [category, granularity, sku, rowCount]);

  const loadSample = useCallback(() => {
    setBusy(true);
    setError(null);
    api
      .forecastSample(category)
      .then((r) => {
        setSkus(r.skus);
        setRowCount(r.count);
        loadData(category);
      })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  }, [category, loadData]);

  const loadBundled = useCallback(
    (name) => {
      setBusy(true);
      setError(null);
      api
        .forecastBundled(name, category)
        .then((r) => {
          setSkus(r.skus);
          setRowCount(r.count);
          loadData(category);
        })
        .catch((e) => setError(e.message))
        .finally(() => setBusy(false));
    },
    [category, loadData],
  );

  const clearData = useCallback(() => {
    if (!window.confirm(`Clear all demand data for ${category}? This cannot be undone.`)) return;
    api.forecastClear(category).then(() => {
      setSkus([]);
      setRows([]);
      setRowCount(0);
      setResult(null);
    });
  }, [category]);

  const addDraftRow = () => setDraft((d) => [...d, { date: '', sku: '', quantity: '' }]);
  const updateDraft = (i, key, val) =>
    setDraft((d) => d.map((r, idx) => (idx === i ? { ...r, [key]: val } : r)));
  const removeDraft = (i) => setDraft((d) => d.filter((_, idx) => idx !== i));

  const submitDraft = useCallback(() => {
    const clean = draft
      .filter((r) => r.date && r.sku && r.quantity !== '')
      .map((r) => ({ date: r.date, sku: r.sku.trim(), quantity: Number(r.quantity) }));
    if (!clean.length) {
      setDraftErr('Add at least one complete row (date, SKU, quantity).');
      return;
    }
    setDraftBusy(true);
    setDraftErr(null);
    api
      .forecastUpsert({ category, mode: 'append', rows: clean })
      .then((r) => {
        setSkus(r.skus);
        setRowCount(r.count);
        setDraft([{ date: '', sku: '', quantity: '' }]);
        loadData(category);
      })
      .catch((e) => setDraftErr(e.message))
      .finally(() => setDraftBusy(false));
  }, [draft, category, loadData]);

  const activeModelData = useMemo(
    () => result?.models.find((m) => m.model === activeModel) || null,
    [result, activeModel],
  );

  if (metaErr) return <ErrorState error={metaErr} />;
  if (!meta) {
    return (
      <div className="flex flex-1 items-center justify-center gap-3 p-16 text-sm text-muted">
        <Spinner /> Loading Demand Forecasting…
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-[1680px] flex-1 flex-col gap-5 px-4 py-5 sm:px-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-bold tracking-tight">Demand Forecasting</h1>
          <p className="text-[12px] text-faint">
            Seven models, side by side, across Finished Goods, Semi-Finished Goods and Raw Material.
          </p>
        </div>
        <button type="button" className="btn text-xs" onClick={() => setShowEntry((v) => !v)}>
          <Icon name="table" size={14} /> {showEntry ? 'Hide data entry' : 'Manage demand data'}
        </button>
      </div>

      {/* category tabs */}
      <div className="flex flex-wrap items-center gap-2">
        {meta.categories.map((c) => (
          <button
            key={c.id}
            type="button"
            onClick={() => setCategory(c.id)}
            className={`chip !text-[12px] !normal-case ${
              category === c.id ? '!border-accent !text-accent' : ''
            }`}
          >
            {c.id} <span className="text-faint">· {c.name}</span>
          </button>
        ))}
        <div className="ml-auto">
          <Toggle
            label="granularity"
            value={granularity}
            onChange={setGranularity}
            options={meta.granularities.map((g) => ({ value: g, label: GRAN_LABELS[g] }))}
          />
        </div>
      </div>

      {showEntry && (
        <DataEntry
          category={category}
          rows={rows}
          rowCount={rowCount}
          skus={skus}
          bundled={meta.bundled}
          draft={draft}
          draftErr={draftErr}
          draftBusy={draftBusy}
          onAdd={addDraftRow}
          onUpdate={updateDraft}
          onRemove={removeDraft}
          onSubmit={submitDraft}
          onSample={loadSample}
          onBundled={loadBundled}
          onClear={clearData}
          busy={busy}
        />
      )}

      {rowCount === 0 && !showEntry && (
        <EmptyState
          title={`No demand data yet for ${category}`}
          hint="Add historical rows or load sample data to see the seven forecasting models compared."
          action={
            <div className="flex gap-2">
              <button type="button" className="btn btn-primary text-xs" onClick={loadSample}>
                Load sample data
              </button>
              <button type="button" className="btn text-xs" onClick={() => setShowEntry(true)}>
                Enter data manually
              </button>
            </div>
          }
        />
      )}

      {error && <ErrorState error={error} onRetry={runForecast} />}

      {rowCount > 0 && (
        <>
          {/* SKU drill-down */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="label">Drill down</span>
            <button
              type="button"
              onClick={() => setSku('__all__')}
              className={`chip !text-[12px] !normal-case ${sku === '__all__' ? '!border-accent !text-accent' : ''}`}
            >
              Category total
            </button>
            {skus.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setSku(s)}
                className={`chip !text-[12px] !normal-case ${sku === s ? '!border-accent !text-accent' : ''}`}
              >
                {s}
              </button>
            ))}
          </div>

          {busy && !result && (
            <div className="flex flex-1 items-center justify-center gap-3 p-16 text-sm text-muted">
              <Spinner /> Running seven forecasting models…
            </div>
          )}

          {result && (
            <>
              <div className="panel flex flex-wrap items-center gap-4 p-4">
                <Stat label="Pattern detected" value={result.pattern.replace('_', ' ')} />
                <Stat
                  label="Recommended model"
                  value={meta.models.find((m) => m.id === result.recommended)?.name || result.recommended}
                  accent
                />
                <Stat label="History" value={`${result.actual.labels.length} periods`} />
                <Stat label="Horizon" value={`${result.models[0]?.chart_line.labels.length - result.actual.labels.length} periods ahead`} />
                <p className="ml-auto max-w-md text-[11px] text-faint">
                  All seven models are always computed — the badge only flags the model this
                  SKU/category's demand pattern best matches.
                </p>
              </div>

              {/* metrics table */}
              <MetricsTable
                models={result.models}
                active={activeModel}
                onSelect={(m) => {
                  setActiveModel(m);
                  setChartTab('line');
                }}
              />

              {/* future demand — all 7 models' upcoming period values, side by side */}
              <FutureDemandTable
                sku={sku}
                futureLabels={result.future_labels}
                models={result.models}
                recommended={result.recommended}
              />

              {activeModelData && (
                <ModelChartPanel
                  model={activeModelData}
                  granularity={granularity}
                  tab={chartTab}
                  onTab={setChartTab}
                  advanced={advanced}
                  onAdvanced={setAdvanced}
                />
              )}

              {/* 3D overview: category surface (Time x SKU) or single-SKU ribbon (Time x Model) */}
              <div className="panel p-4">
                <div className="mb-2 flex items-center justify-between">
                  <h3 className="text-sm font-semibold">
                    {sku === '__all__' && result.surface3d
                      ? '3D surface — Time × SKU × Demand'
                      : '3D ribbon — Time × Model × Demand'}
                  </h3>
                  <span className="chip">Surface plot</span>
                </div>
                <div className="relative h-[380px]">
                  <ChartView
                    slide={{
                      id: `surface-${category}-${granularity}-${sku}`,
                      type: 'surface3d',
                      title: 'Demand surface',
                      data: sku === '__all__' && result.surface3d ? result.surface3d : result.ribbon3d,
                    }}
                  />
                </div>
                <p className="mt-2 text-[11px] text-faint">
                  {sku === '__all__' && result.surface3d
                    ? 'Every SKU\u2019s history and its recommended-model forecast, so build-ups and drop-offs across items are visible at once.'
                    : 'All seven models\u2019 forecasts for this SKU, layered so their shapes can be compared directly.'}
                </p>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}

function Stat({ label, value, accent }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className={`text-sm font-semibold capitalize ${accent ? 'text-accent' : 'text-ink'}`}>{value}</div>
    </div>
  );
}

function MetricsTable({ models, active, onSelect }) {
  const [sortKey, setSortKey] = useState(null);
  const [sortDesc, setSortDesc] = useState(false);
  const sorted = useMemo(() => {
    if (!sortKey) return models;
    return [...models].sort((a, b) => {
      const av = a.metrics[sortKey] ?? Infinity;
      const bv = b.metrics[sortKey] ?? Infinity;
      return sortDesc ? bv - av : av - bv;
    });
  }, [models, sortKey, sortDesc]);

  const toggleSort = (key) => {
    if (sortKey === key) setSortDesc((d) => !d);
    else {
      setSortKey(key);
      setSortDesc(false);
    }
  };

  return (
    <div className="panel overflow-x-auto">
      <table className="w-full min-w-[560px] border-collapse text-left text-[12px]">
        <thead>
          <tr className="border-b border-line">
            <th className="px-3 py-2 label">Model</th>
            {['mae', 'rmse', 'mape'].map((k) => (
              <th key={k} className="cursor-pointer px-3 py-2 label" onClick={() => toggleSort(k)}>
                {k.toUpperCase()} {sortKey === k ? (sortDesc ? '↓' : '↑') : ''}
              </th>
            ))}
            <th className="px-3 py-2 label">Best suited for</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((m) => (
            <tr
              key={m.model}
              onClick={() => onSelect(m.model)}
              className={`cursor-pointer border-b border-line/50 hover:bg-line/25 ${
                active === m.model ? 'bg-accent/[0.08]' : ''
              }`}
            >
              <td className="px-3 py-2 font-medium text-ink">
                {m.name}
                {m.is_recommended && (
                  <span className="ml-2 chip !border-accent !text-accent !normal-case">Recommended</span>
                )}
              </td>
              <td className="num px-3 py-2 text-muted">{fmtNum(m.metrics.mae) ?? '—'}</td>
              <td className="num px-3 py-2 text-muted">{fmtNum(m.metrics.rmse) ?? '—'}</td>
              <td className="num px-3 py-2 text-muted">{m.metrics.mape != null ? `${m.metrics.mape}%` : '—'}</td>
              <td className="px-3 py-2 text-faint">{m.best_for}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* Future Demand — every future period, all seven models' predicted quantity
   side by side, for whatever is currently drilled into (category total or a
   single SKU). This is a table, not a chart: the point is exact numbers a
   planner can act on, with the Recommended column called out. */
function FutureDemandTable({ sku, futureLabels, models, recommended }) {
  const [csvBusy, setCsvBusy] = useState(false);

  const downloadCsv = () => {
    setCsvBusy(true);
    try {
      const header = ['Period', ...models.map((m) => m.name)];
      const lines = futureLabels.map((label, i) => [label, ...models.map((m) => m.future[i])]);
      const csv = [header, ...lines].map((row) => row.join(',')).join('\n');
      const blob = new Blob([csv], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `future-demand-${sku === '__all__' ? 'category' : sku}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setCsvBusy(false);
    }
  };

  return (
    <div className="panel overflow-x-auto p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">
            Future demand — {sku === '__all__' ? 'category total' : sku}
          </h3>
          <p className="text-[11px] text-faint">
            Every upcoming period, all seven models' predicted quantity, calculated from the
            history above.
          </p>
        </div>
        <button type="button" className="btn text-xs" onClick={downloadCsv} disabled={csvBusy}>
          <Icon name="download" size={14} /> Export CSV
        </button>
      </div>
      <table className="w-full min-w-[720px] border-collapse text-left text-[12px]">
        <thead>
          <tr className="border-b border-line">
            <th className="px-3 py-2 label">Period</th>
            {models.map((m) => (
              <th key={m.model} className="num px-3 py-2 label">
                {m.name}
                {m.model === recommended && (
                  <span className="ml-1 chip !border-accent !text-accent !normal-case">Rec.</span>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {futureLabels.map((label, i) => (
            <tr key={label} className="border-b border-line/50">
              <td className="px-3 py-2 font-medium text-ink">{label}</td>
              {models.map((m) => (
                <td
                  key={m.model}
                  className={`num px-3 py-2 ${m.model === recommended ? 'font-semibold text-accent' : 'text-muted'}`}
                >
                  {fmtNum(m.future[i]) ?? '—'}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const TABS = [
  { value: 'line', label: 'Line' },
  { value: 'bar', label: 'Bar' },
  { value: 'pie', label: 'Pie' },
];

/* Reusable panel: renders the required Line / Bar / Pie views for one model's
   forecast (the 4th chart — the 3D surface — is shared across all models above,
   since a per-model surface would just repeat the same seven forecast lines). */
function ModelChartPanel({ model, tab, onTab, advanced, onAdvanced }) {
  const slide = useMemo(() => {
    if (tab === 'line') return { id: `${model.model}-line`, type: 'multi_line', title: 'Actual vs forecast', data: model.chart_line };
    if (tab === 'bar') return { id: `${model.model}-bar`, type: 'grouped_bar', title: 'Period comparison', data: model.chart_bar };
    return { id: `${model.model}-pie`, type: 'pie', title: 'Share of forecast horizon', data: { ...model.chart_pie, datasets: [{ label: 'Share of forecast', data: model.chart_pie.datasets[0].data }] } };
  }, [tab, model]);

  return (
    <div className="panel p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">
            {model.name}
            {model.is_recommended && <span className="ml-2 chip !border-accent !text-accent !normal-case">Recommended</span>}
          </h3>
          <p className="text-[11px] text-faint">Best for: {model.best_for}</p>
        </div>
        <Toggle label="chart-tab" value={tab} onChange={onTab} options={TABS} />
      </div>
      <div className="relative h-[320px]">
        <ChartView slide={slide} />
      </div>
      <button type="button" className="btn-ghost mt-2 text-[11px]" onClick={() => onAdvanced((v) => !v)}>
        <Icon name={advanced ? 'expand' : 'sliders'} size={13} /> {advanced ? 'Hide' : 'Show'} advanced parameters
      </button>
      {advanced && (
        <pre className="scroll-y mt-2 max-h-40 overflow-auto rounded-lg border border-line bg-surface p-3 text-[11px] text-muted">
          {JSON.stringify(model.meta, null, 2)}
        </pre>
      )}
    </div>
  );
}

function DataEntry({ category, rows, rowCount, skus, bundled, draft, draftErr, draftBusy, onAdd, onUpdate, onRemove, onSubmit, onSample, onBundled, onClear, busy }) {
  return (
    <div className="panel space-y-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">Historical demand — {category}</h3>
          <p className="text-[11px] text-faint">
            {rowCount} row{rowCount === 1 ? '' : 's'} · {skus.length} SKU{skus.length === 1 ? '' : 's'} loaded.
            Fields: Date, Quantity, SKU / Item identifier.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn text-xs" onClick={onSample} disabled={busy}>
            Load synthetic sample
          </button>
          {(bundled || []).map((b) => (
            <button
              key={b.id}
              type="button"
              className="btn text-xs"
              onClick={() => onBundled(b.id)}
              disabled={busy}
              title={b.description}
            >
              Load {b.label}
            </button>
          ))}
          <button type="button" className="btn text-xs" onClick={onClear} disabled={!rowCount}>
            Clear all
          </button>
        </div>
      </div>

      <div className="space-y-2">
        {draft.map((r, i) => (
          <div key={i} className="flex flex-wrap items-center gap-2">
            <input
              type="date"
              className="field !w-40"
              value={r.date}
              onChange={(e) => onUpdate(i, 'date', e.target.value)}
            />
            <input
              type="text"
              placeholder="SKU / item"
              className="field !w-40"
              value={r.sku}
              onChange={(e) => onUpdate(i, 'sku', e.target.value)}
            />
            <input
              type="number"
              min="0"
              placeholder="Quantity"
              className="field !w-28"
              value={r.quantity}
              onChange={(e) => onUpdate(i, 'quantity', e.target.value)}
            />
            <button type="button" className="btn-ghost rounded-md p-1.5" onClick={() => onRemove(i)} aria-label="Remove row">
              <Icon name="close" size={14} />
            </button>
          </div>
        ))}
        <div className="flex items-center gap-2">
          <button type="button" className="btn text-xs" onClick={onAdd}>
            + Add row
          </button>
          <button type="button" className="btn btn-primary text-xs" onClick={onSubmit} disabled={draftBusy}>
            {draftBusy ? <Spinner size={13} /> : 'Save rows'}
          </button>
        </div>
        {draftErr && <p className="text-[11px] text-warn">{draftErr}</p>}
      </div>

      {rows.length > 0 && (
        <details className="text-[12px]">
          <summary className="cursor-pointer text-faint">View loaded rows ({rows.length})</summary>
          <div className="scroll-y mt-2 max-h-56 overflow-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-line">
                  <th className="px-2 py-1 label">Date</th>
                  <th className="px-2 py-1 label">SKU</th>
                  <th className="px-2 py-1 label">Quantity</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, 200).map((r, i) => (
                  <tr key={i} className="border-b border-line/40">
                    <td className="px-2 py-1 text-muted">{r.date}</td>
                    <td className="px-2 py-1 text-muted">{r.sku}</td>
                    <td className="num px-2 py-1 text-ink">{r.quantity}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}
