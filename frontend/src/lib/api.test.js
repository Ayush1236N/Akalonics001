import { beforeEach, describe, expect, it, vi } from 'vitest';
import { API, api, auth } from './api';

const jsonRes = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

describe('api client', () => {
  beforeEach(() => {
    auth.clear();
    vi.restoreAllMocks();
  });

  it('resolves a usable API base', () => {
    expect(API).toMatch(/^https?:\/\//);
    expect(API.endsWith('/')).toBe(false);
  });

  it('stores the session returned by login and sends a bearer token', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonRes({ user: { id: 'u1', email: 'a@b.c' }, access_token: 'tok', refresh_token: 'r', csrf_token: 'c' }),
      )
      .mockResolvedValueOnce(jsonRes({ samples: [] }));
    vi.stubGlobal('fetch', fetchMock);

    await api.login('a@b.c', 'password12');
    expect(auth.user.email).toBe('a@b.c');

    await api.samples();
    const [, init] = fetchMock.mock.calls[1];
    expect(init.headers.Authorization).toBe('Bearer tok');
    expect(init.headers['x-csrf-token']).toBe('c');
    expect(init.credentials).toBe('include');
  });

  it('refreshes once on a 401 and retries the original request', async () => {
    auth.set({ user: { id: 'u1' }, access_token: 'stale', refresh_token: 'r1', csrf_token: 'c' });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonRes({ detail: 'expired' }, 401))
      .mockResolvedValueOnce(jsonRes({ access_token: 'fresh', refresh_token: 'r2', user: { id: 'u1' } }))
      .mockResolvedValueOnce(jsonRes({ datasets: [] }));
    vi.stubGlobal('fetch', fetchMock);

    const out = await api.datasets();
    expect(out).toEqual({ datasets: [] });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][0]).toContain('/api/auth/refresh');
    expect(fetchMock.mock.calls[2][1].headers.Authorization).toBe('Bearer fresh');
    expect(auth.session.access_token).toBe('fresh');
  });

  it('clears the session and notifies when the refresh also fails', async () => {
    auth.set({ user: { id: 'u1' }, access_token: 'stale', refresh_token: 'bad' });
    const onUnauthorized = vi.fn();
    auth.onUnauthorized(onUnauthorized);
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonRes({ detail: 'expired' }, 401))
        .mockResolvedValueOnce(jsonRes({ detail: 'nope' }, 401)),
    );

    await expect(api.datasets()).rejects.toThrow();
    expect(auth.session).toBeNull();
    expect(onUnauthorized).toHaveBeenCalled();
    auth.onUnauthorized(null);
  });

  it('surfaces the server detail message and status', async () => {
    auth.set({ user: {}, access_token: 't' });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonRes({ detail: 'Dataset is not available.' }, 404)));
    await expect(api.dataset('x')).rejects.toThrow('Dataset is not available.');
  });

  it('signs GET-only export urls with the access token', () => {
    auth.set({ user: {}, access_token: 'tok123' });
    const url = api.exportUrl('ds1', 'Sheet 1', 'pptx');
    expect(url).toContain('/api/export/ds1');
    expect(url).toContain('format=pptx');
    expect(url).toContain('access_token=tok123');
    expect(api.pngUrl('ds1', 'bar-0')).toContain('/api/export/png/ds1/bar-0');
  });

  it('streams job progress and resolves on done', async () => {
    auth.set({ user: {}, access_token: 'tok' });
    let instance;
    class FakeES {
      constructor(url) {
        this.url = url;
        instance = this;
      }
      close() {
        this.closed = true;
      }
    }
    vi.stubGlobal('EventSource', FakeES);
    const seen = [];
    const done = api.progress('job1', (e) => seen.push(e.stage));
    instance.onmessage({ data: JSON.stringify({ stage: 'parsing', pct: 20 }) });
    instance.onmessage({ data: JSON.stringify({ stage: 'done', pct: 100 }) });
    await expect(done).resolves.toMatchObject({ stage: 'done' });
    expect(seen).toEqual(['parsing', 'done']);
    expect(instance.closed).toBe(true);
    expect(instance.url).toContain('access_token=tok');
  });

  it('rejects when the progress stream reports an error stage', async () => {
    let instance;
    vi.stubGlobal(
      'EventSource',
      class {
        constructor() {
          instance = this;
        }
        close() {}
      },
    );
    const p = api.progress('job2', () => {});
    instance.onmessage({ data: JSON.stringify({ stage: 'error', detail: 'parse blew up' }) });
    await expect(p).rejects.toThrow('parse blew up');
  });

  it('logout clears local state even when the server call fails', async () => {
    auth.set({ user: { id: 'u' }, access_token: 't', refresh_token: 'r' });
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    await api.logout();
    expect(auth.session).toBeNull();
  });
});
