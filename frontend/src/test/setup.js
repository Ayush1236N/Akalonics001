import '@testing-library/jest-dom/vitest';
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  try {
    sessionStorage.clear();
  } catch {
    /* ignore */
  }
});

// jsdom has no EventSource; tests install their own fake when needed.
if (!globalThis.EventSource) {
  globalThis.EventSource = class {
    constructor() {
      this.onmessage = null;
      this.onerror = null;
    }
    close() {}
  };
}
