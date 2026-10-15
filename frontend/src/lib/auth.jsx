import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { api, auth as store } from './api';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => store.user);
  const [status, setStatus] = useState('loading'); // loading | anon | authed
  const [config, setConfig] = useState({ guest_mode: true, nl_charts: false, chart_types: 37 });
  const [apiUp, setApiUp] = useState(null);

  // Boot: probe the API, then validate any stored session.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const cfg = await api.config();
        if (!alive) return;
        setConfig(cfg);
        setApiUp(true);
      } catch {
        if (!alive) return;
        setApiUp(false);
        setStatus('anon');
        return;
      }
      if (store.session?.access_token) {
        try {
          const me = await api.me();
          if (!alive) return;
          setUser(me.user);
          setStatus('authed');
          return;
        } catch {
          store.clear();
        }
      }
      if (alive) {
        setUser(null);
        setStatus('anon');
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // A refresh failure anywhere in the app drops us back to the sign-in screen.
  useEffect(() => {
    store.onUnauthorized(() => {
      setUser(null);
      setStatus('anon');
    });
    return () => store.onUnauthorized(null);
  }, []);

  const adopt = useCallback((session) => {
    setUser(session.user);
    setStatus('authed');
    return session.user;
  }, []);

  const value = useMemo(
    () => ({
      user,
      status,
      config,
      apiUp,
      isGuest: !!user?.is_guest,
      login: (email, password) => api.login(email, password).then(adopt),
      signup: (email, password) => api.signup(email, password).then(adopt),
      guest: () => api.guest().then(adopt),
      logout: async () => {
        await api.logout();
        setUser(null);
        setStatus('anon');
      },
    }),
    [user, status, config, apiUp, adopt],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
