import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// globals are off (vite.config.ts test.globals = false), so @testing-library
// cannot auto-register its afterEach — do it here or DOM leaks between tests.
afterEach(() => cleanup());

// jsdom has neither ResizeObserver (recharts ResponsiveContainer queries it)
// nor matchMedia; stub the observer so the Capacity chart mounts under test.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (typeof globalThis.ResizeObserver === 'undefined') {
  (globalThis as Record<string, unknown>).ResizeObserver = ResizeObserverStub;
}
if (typeof globalThis.matchMedia === 'undefined') {
  (globalThis as Record<string, unknown>).matchMedia = () => ({
    matches: false,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return false;
    },
  });
}
