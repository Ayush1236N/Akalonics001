import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AuthProvider, useAuth } from './auth';
import { api, auth as store } from './api';

function Probe() {
  const { status, user, isGuest, guest, logout, config } = useAuth();
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="user">{user ? (isGuest ? 'guest' : user.email) : 'none'}</span>
      <span data-testid="flag">{String(config.nl_charts)}</span>
      <button type="button" onClick={() => guest()}>
        go guest
      </button>
      <button type="button" onClick={() => logout()}>
        sign out
      </button>
    </div>
  );
}

const CONFIG = { guest_mode: true, nl_charts: true, chart_types: 37 };

describe('AuthProvider', () => {
  it('boots to anon when no session is stored', async () => {
    vi.spyOn(api, 'config').mockResolvedValue(CONFIG);
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anon'));
    expect(screen.getByTestId('user')).toHaveTextContent('none');
    expect(screen.getByTestId('flag')).toHaveTextContent('true');
  });

  it('validates a stored session with /me on boot', async () => {
    store.set({ user: { id: 'u1', email: 'stored@example.com' }, access_token: 't' });
    vi.spyOn(api, 'config').mockResolvedValue(CONFIG);
    vi.spyOn(api, 'me').mockResolvedValue({ user: { id: 'u1', email: 'stored@example.com' } });
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authed'));
    expect(screen.getByTestId('user')).toHaveTextContent('stored@example.com');
    store.clear();
  });

  it('drops a stale stored session', async () => {
    store.set({ user: { id: 'u1', email: 'x@y.z' }, access_token: 'dead' });
    vi.spyOn(api, 'config').mockResolvedValue(CONFIG);
    vi.spyOn(api, 'me').mockRejectedValue(new Error('401'));
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anon'));
    expect(store.session).toBeNull();
  });

  it('guest sign-in then sign-out round-trips', async () => {
    vi.spyOn(api, 'config').mockResolvedValue(CONFIG);
    vi.spyOn(api, 'guest').mockResolvedValue({ user: { id: 'g1', is_guest: true }, access_token: 't' });
    vi.spyOn(api, 'logout').mockResolvedValue(undefined);
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anon'));
    await userEvent.click(screen.getByText('go guest'));
    await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent('guest'));
    await userEvent.click(screen.getByText('sign out'));
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anon'));
  });

  it('reports the API as down when config fails', async () => {
    vi.spyOn(api, 'config').mockRejectedValue(new Error('offline'));
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anon'));
  });
});
