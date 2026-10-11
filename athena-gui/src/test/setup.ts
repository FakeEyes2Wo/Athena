import "@testing-library/jest-dom/vitest";

// Simulate the Tauri runtime for all tests so invoke/listen work.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(window as any).__TAURI_INTERNALS__ = {};

// Node >= 25 ships its own global Web Storage, which shadows jsdom's and is
// undefined unless ``--localstorage-file`` is given. Restore jsdom's storage so
// tests behave the same on Node 20 and newer runtimes.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const dom = (globalThis as any).jsdom as { window: Window } | undefined;
for (const key of ["localStorage", "sessionStorage"] as const) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  if (dom && typeof (globalThis as any)[key]?.clear !== "function") {
    Object.defineProperty(globalThis, key, { configurable: true, value: dom.window[key] });
  }
}
