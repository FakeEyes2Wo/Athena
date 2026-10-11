import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

// Simulate the Tauri runtime for all tests so invoke/listen work.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(window as any).__TAURI_INTERNALS__ = {};

// Primer React 依赖的浏览器 API 在 jsdom 中缺失。内容与 @primer/react/test-helpers
// 等价，但那份文件使用 Jest 全局变量，无法在 Vitest 中导入。
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

class IntersectionObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const globals = globalThis as any;
globals.ResizeObserver ??= ResizeObserverStub;
globals.IntersectionObserver ??= IntersectionObserverStub;
globals.CSS ??= {};
globals.CSS.supports ??= () => false;
globals.CSS.escape ??= (value: string) => value;
// Primer v38 的 Dialog 渲染为 div[role=dialog]，不需要 HTMLDialogElement 补丁。
globals.Element.prototype.scrollIntoView ??= function scrollIntoView() {};
if (typeof window.matchMedia !== "function") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  });
}
