// API base resolution, in priority order:
//   1. VITE_API_BASE (docker compose serves the API at a relative /api via nginx)
//   2. __PORT_8000__ placeholder, rewritten by deploy_website to the sandbox proxy URL
//   3. localhost:8000 for plain local dev
const RAW = '__PORT_8000__';
const ENV_BASE = (import.meta?.env?.VITE_API_BASE ?? '').trim();
export const API = ENV_BASE
  ? ENV_BASE.replace(/\/$/, '')
  : RAW.startsWith('__')
    ? 'http://localhost:8000'
    : RAW.replace(/\/$/, '');

/* ---------------------------------------------------------------- tokens --
   The deployed preview runs inside a sandboxed iframe where cookies and
   localStorage are unreliable, so tokens are also returned in the JSON body
   and held in memory (mirrored to sessionStorage when it is available). */
const KEY = 'akalonics.session';
let session = null;
let onUnauthorized = null;

function readStore() {
  try {
    const raw = globalThis.sessionStorage?.getItem(KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function writeStore(value) {
  try {
    if (value) globalThis.sessionStorage?.setItem(KEY, JSON.stringify(value));
    else globalThis.sessionStorage?.removeItem(KEY);
  } catch {
    /* storage blocked in sandboxed iframes; memory-only is fine */
  }
}

export const auth = {
  get session() {
    if (!session) session = readStore();
    return session;
  },
  set(next) {
    session = next;
    writeStore(next);
    return next;
  },
  clear() {
    session = null;
    writeStore(null);
  },
  get user() {
    return auth.session?.user ?? null;
  },
  onUnauthorized(fn) {
    onUnauthorized = fn;
  },
};

function authHeaders(extra = {}) {
  const s = auth.session;
  const h = { ...extra };
  if (s?.access_token) h.Authorization = `Bearer ${s.access_token}`;
  if (s?.csrf_token) h['x-csrf-token'] = s.csrf_token;
  return h;
}

async function parseError(res) {
  let detail = `Request failed (${res.status})`;
  try {
    const body = await res.json();
    if (body?.detail) {
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    }
  } catch {
    /* non-JSON error body */
  }
  const err = new Error(detail);
  err.status = res.status;
  return err;
}

async function handle(res) {
  if (!res.ok) throw await parseError(res);
  return res.json();
}

let refreshing = null;

async function refreshOnce() {
  const token = auth.session?.refresh_token;
  if (!token) return null;
  refreshing =
    refreshing ||
    fetch(`${API}/api/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: token }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
      .finally(() => {
        refreshing = null;
      });
  const next = await refreshing;
  if (next?.access_token) return auth.set({ ...auth.session, ...next });
  auth.clear();
  onUnauthorized?.();
  return null;
}

// Every protected call: try -> on 401 refresh silently once -> retry -> surface.
async function req(path, { method = 'GET', body, form, retry = true } = {}) {
  const init = { method, credentials: 'include', headers: authHeaders() };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  if (form) init.body = form;
  const res = await fetch(`${API}${path}`, init);
  if (res.status === 401 && retry && auth.session?.refresh_token) {
    if (await refreshOnce()) return req(path, { method, body, form, retry: false });
  }
  if (res.status === 401) {
    auth.clear();
    onUnauthorized?.();
  }
  return handle(res);
}

/* --------------------------------------------------------------- signed urls --
   EventSource and window.open cannot send an Authorization header, so GET-only
   endpoints accept the access token as a query parameter. */
function signed(path, params = {}) {
  const q = new URLSearchParams(params);
  const token = auth.session?.access_token;
  if (token) q.set('access_token', token);
  return `${API}${path}?${q}`;
}

export const api = {
  health: () => fetch(`${API}/api/health`).then(handle),
  config: () => fetch(`${API}/api/config`).then(handle),

  signup: (email, password) =>
    fetch(`${API}/api/auth/signup`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    })
      .then(handle)
      .then(auth.set),
  login: (email, password) =>
    fetch(`${API}/api/auth/login`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    })
      .then(handle)
      .then(auth.set),
  guest: () =>
    fetch(`${API}/api/auth/guest`, { method: 'POST', credentials: 'include' })
      .then(handle)
      .then(auth.set),
  me: () => req('/api/auth/me'),
  logout: async () => {
    try {
      await req('/api/auth/logout', { method: 'POST', retry: false });
    } catch {
      /* logging out locally matters more than the server round-trip */
    }
    auth.clear();
  },

  samples: () => req('/api/samples'),
  chartTypes: () => req('/api/chart-types'),
  datasets: () => req('/api/datasets'),
  dataset: (id) => req(`/api/datasets/${id}`),

  // Uploads return a job; callers poll progress() then fetch the dataset.
  upload: (file, onProgress) =>
    new Promise((resolve, reject) => {
      const form = new FormData();
      form.append('file', file);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${API}/api/upload`);
      xhr.withCredentials = true;
      const headers = authHeaders();
      Object.entries(headers).forEach(([k, v]) => xhr.setRequestHeader(k, v));
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress((e.loaded / e.total) * 100);
      };
      xhr.onload = () => {
        try {
          const body = JSON.parse(xhr.responseText || '{}');
          if (xhr.status >= 200 && xhr.status < 300) resolve(body);
          else reject(new Error(body.detail || `Upload failed (${xhr.status})`));
        } catch {
          reject(new Error('The server returned an unreadable response.'));
        }
      };
      xhr.onerror = () => reject(new Error('Network error while uploading. Is the API running?'));
      xhr.send(form);
    }),
  sample: (name) => req(`/api/sample/${encodeURIComponent(name)}`, { method: 'POST' }),
  rows: (id, params) => req(`/api/rows/${id}?${new URLSearchParams(params)}`),
  analyze: (body) => req('/api/analyze', { method: 'POST', body }),
  aggregate: (body) => req('/api/aggregate', { method: 'POST', body }),
  describe: (body) => req('/api/describe', { method: 'POST', body }),
  job: (id) => req(`/api/jobs/${id}`),
  startAnalyze: (body) => req('/api/jobs', { method: 'POST', body }),
  nlSuggestions: (id, sheet) =>
    req(`/api/nl-suggestions/${id}?${new URLSearchParams({ sheet: sheet || '' })}`),
  nlChart: (body, signal) =>
    fetch(`${API}/api/nl-chart`, {
      method: 'POST',
      credentials: 'include',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
      signal,
    }).then(handle),

  exportUrl: (id, sheet, format) => signed(`/api/export/${id}`, { sheet: sheet || '', format }),
  pngUrl: (id, slideId, sheet) =>
    signed(`/api/export/png/${id}/${slideId}`, { sheet: sheet || '' }),

  /* Real job progress: SSE stream of the worker's DB-backed state machine
     (queued -> parsing -> profiling -> planning -> done|error). */
  progress: (jobId, onEvent) =>
    new Promise((resolve, reject) => {
      const es = new EventSource(signed(`/api/progress/${jobId}`));
      const finish = (fn, arg) => {
        es.close();
        fn(arg);
      };
      es.onmessage = (e) => {
        let payload;
        try {
          payload = JSON.parse(e.data);
        } catch {
          return;
        }
        onEvent?.(payload);
        if (payload.stage === 'done') finish(resolve, payload);
        else if (payload.stage === 'error') {
          finish(reject, new Error(payload.detail || 'The analysis pipeline failed.'));
        }
      };
      es.onerror = () => finish(reject, new Error('Lost connection to the analysis stream.'));
    }),

  // Demand Forecasting — T7-F / T7-SFG / T7-R, 7 models x 5 granularities.
  forecastMeta: () => req('/api/forecast/meta'),
  forecastData: (category) => req(`/api/forecast/data/${encodeURIComponent(category)}`),
  forecastUpsert: (body) => req('/api/forecast/data', { method: 'POST', body }),
  forecastClear: (category) =>
    req(`/api/forecast/data/${encodeURIComponent(category)}`, { method: 'DELETE' }),
  forecastSample: (category) =>
    req(`/api/forecast/sample/${encodeURIComponent(category)}`, { method: 'POST' }),
  forecastBundled: (name, category) =>
    req(`/api/forecast/bundled/${encodeURIComponent(name)}`, { method: 'POST', body: { category } }),
  forecastRun: (body) => req('/api/forecast/run', { method: 'POST', body }),
};

export function downloadUrl(url) {
  // The sandbox proxy blocks blob downloads, so hand the URL to the browser directly.
  window.open(url, '_blank', 'noopener,noreferrer');
}
