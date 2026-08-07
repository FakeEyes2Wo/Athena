import "@testing-library/jest-dom/vitest";

// Simulate the Tauri runtime for all tests so invoke/listen work.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(window as any).__TAURI_INTERNALS__ = {};
