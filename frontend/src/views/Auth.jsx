import { useState } from 'react';
import { motion } from 'framer-motion';
import { useAuth } from '../lib/auth';
import { useTheme } from '../lib/theme';
import { ErrorState, Icon, Logo, Spinner } from '../components/ui';

const MODES = {
  login: { title: 'Sign in', cta: 'Sign in', alt: 'signup', altLabel: 'Create an account' },
  signup: { title: 'Create an account', cta: 'Create account', alt: 'login', altLabel: 'I already have an account' },
};

export default function Auth() {
  const { login, signup, guest, config, apiUp } = useAuth();
  const theme = useTheme();
  const [mode, setMode] = useState('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const copy = MODES[mode];

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError('Passwords need at least 8 characters.');
      return;
    }
    setBusy(mode);
    try {
      await (mode === 'login' ? login(email.trim(), password) : signup(email.trim(), password));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  const asGuest = async () => {
    setError(null);
    setBusy('guest');
    try {
      await guest();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="relative flex min-h-screen flex-col items-center justify-center overflow-hidden px-5 py-14">
      <div className="grid-bg absolute inset-0 opacity-70" aria-hidden="true" />
      <div
        className="pointer-events-none absolute left-1/2 top-0 h-[420px] w-[720px] -translate-x-1/2 rounded-full bg-accent/[0.09] blur-[130px]"
        aria-hidden="true"
      />

      <motion.div
        className="panel relative z-10 w-full max-w-[420px] p-7"
        initial={{ opacity: 0, y: 18 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: theme.reducedMotion ? 0 : 0.5, ease: [0.22, 1, 0.36, 1] }}
      >
        <Logo />
        <h1 className="mt-6 text-[22px] font-black leading-tight">{copy.title}</h1>
        <p className="mt-2 text-[13px] leading-relaxed text-muted">
          AkaLonics keeps every dataset, deck and export scoped to your account &mdash;{' '}
          {config.chart_types || 37} chart types, four export formats.
        </p>

        {apiUp === false && (
          <div className="mt-4 rounded-lg border border-warn/40 bg-warn/[0.08] px-3 py-2 text-[12px] text-ink">
            The API is unreachable. Start the backend and reload.
          </div>
        )}

        <form className="mt-6 space-y-4" onSubmit={submit}>
          <label className="block">
            <span className="label">email</span>
            <input
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="field mt-2"
              placeholder="you@company.com"
              data-testid="input-email"
            />
          </label>
          <label className="block">
            <span className="label">password</span>
            <input
              type="password"
              required
              minLength={8}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="field mt-2"
              placeholder="at least 8 characters"
              data-testid="input-password"
            />
          </label>

          {error && <ErrorState error={error} compact />}

          <button
            type="submit"
            className="btn btn-primary w-full justify-center"
            disabled={!!busy}
            data-testid={mode === 'login' ? 'button-login' : 'button-signup'}
          >
            {busy === mode ? <Spinner /> : <Icon name="arrowRight" size={16} />} {copy.cta}
          </button>
        </form>

        <div className="mt-5 flex items-center gap-3 text-[11px] text-faint">
          <span className="h-px flex-1 bg-line" />
          or
          <span className="h-px flex-1 bg-line" />
        </div>

        {config.guest_mode !== false && (
          <button
            type="button"
            onClick={asGuest}
            className="btn mt-5 w-full justify-center"
            disabled={!!busy}
            data-testid="button-guest"
          >
            {busy === 'guest' ? <Spinner /> : <Icon name="sparkle" size={16} />} Continue as guest
          </button>
        )}
        <p className="mt-3 text-[11px] leading-relaxed text-faint">
          Guest sessions get the full studio and the sample datasets instantly. Nothing is emailed
          and guest data is disposable.
        </p>

        <button
          type="button"
          onClick={() => {
            setMode(copy.alt);
            setError(null);
          }}
          className="btn-ghost mt-5 w-full justify-center rounded-lg py-2 text-[12px]"
          data-testid="button-switch-mode"
        >
          {copy.altLabel}
        </button>
      </motion.div>
    </div>
  );
}
