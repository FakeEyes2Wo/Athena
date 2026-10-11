import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WsBackend, type BackendStatus } from "../ws-backend";

class FakeSocket {
  static OPEN = 1;
  static instances: FakeSocket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  sent: string[] = [];

  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }

  close() {
    this.readyState = 3;
    this.onclose?.();
  }
}

describe("WsBackend connection status", () => {
  const original = globalThis.WebSocket;

  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.instances = [];
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = FakeSocket;
  });

  afterEach(() => {
    vi.useRealTimers();
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = original;
  });

  it("reports connecting, open, closed and reconnects", async () => {
    const backend = new WsBackend();
    const seen: BackendStatus[] = [];
    backend.onStatus((status) => seen.push(status));

    const connected = backend.connect("ws://test");
    FakeSocket.instances[0].open();
    await connected;
    FakeSocket.instances[0].close();

    expect(seen).toEqual(["connecting", "open", "closed"]);

    vi.advanceTimersByTime(1000);
    expect(FakeSocket.instances).toHaveLength(2);
    FakeSocket.instances[1].open();
    expect(seen[seen.length - 1]).toBe("open");
  });

  it("fails in-flight calls when the socket drops", async () => {
    const backend = new WsBackend();
    const connected = backend.connect("ws://test");
    FakeSocket.instances[0].open();
    await connected;

    const call = backend.call("ping");
    await Promise.resolve();
    FakeSocket.instances[0].close();

    await expect(call).rejects.toThrow("后端连接已断开");
  });
});
