import { Component } from 'react';

/* Last-resort guard: a render failure inside a view shows a recoverable panel
   instead of a blank page. */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error('AkaLonics render error', error, info?.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div
        className="flex min-h-screen items-center justify-center p-6"
        data-testid="error-boundary"
      >
        <div className="panel max-w-md space-y-3 p-6 text-center">
          <p className="label">something broke</p>
          <h1 className="text-[18px] font-bold">This view failed to render.</h1>
          <p className="text-[12.5px] leading-relaxed text-muted">
            {error.message || 'Unexpected error.'}
          </p>
          <button
            type="button"
            className="btn btn-accent"
            data-testid="button-error-reload"
            onClick={() => {
              this.setState({ error: null });
              window.location.reload();
            }}
          >
            Reload AkaLonics
          </button>
        </div>
      </div>
    );
  }
}
