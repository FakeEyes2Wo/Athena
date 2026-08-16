/** Direct WebSocket client for the Python gateway (browser preview without Tauri). */

type EventHandler = (kind: string, data: unknown) => void;

interface PendingCall {
  resolve(value: unknown): void;
  reject(error: Error): void;
}

interface ResponseEnvelope {
  request_id: number;
  result?: unknown;
  error?: { code: number; message: string };
}

interface EventEnvelope {
  kind: string;
  data: unknown;
}

/**
 * Gateway URL: ``VITE_GUI_WS_URL`` (完整 ws URL) > ``VITE_GUI_PORT`` (端口) >
 * 默认 ``ws://127.0.0.1:17601``（与后端 ``gui_gateway`` 默认端口一致）。
 */
function gatewayUrl(): string {
  const explicit = import.meta.env?.VITE_GUI_WS_URL as string | undefined;
  if (explicit) return explicit;
  const port = (import.meta.env?.VITE_GUI_PORT as string | undefined) ?? "17601";
  return `ws://127.0.0.1:${port}`;
}

const DEFAULT_URL = gatewayUrl();
const MAX_BACKOFF_MS = 8000;

export class WsBackend {
  private ws: WebSocket | null = null;
  private nextId = 1;
  private pending = new Map<number, PendingCall>();
  private handlers = new Set<EventHandler>();
  private connectPromise: Promise<WebSocket> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private attempts = 0;

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  connect(url: string = DEFAULT_URL): Promise<WebSocket> {
    if (this.connectPromise) return this.connectPromise;
    this.connectPromise = new Promise<WebSocket>((resolve, reject) => {
      const ws = new WebSocket(url);
      let settled = false;
      ws.onopen = () => {
        settled = true;
        this.attempts = 0;
        resolve(ws);
      };
      ws.onmessage = (event) => this.handleMessage(event.data);
      ws.onerror = () => {
        // onerror 之后必然跟 onclose；失败统一由 onclose 处理，避免重复 settle。
      };
      ws.onclose = () => {
        if (this.ws !== ws) return;
        this.ws = null;
        this.connectPromise = null;
        if (!settled) {
          settled = true;
          reject(new Error(`无法连接后端 ${url}，请先启动后端`));
        }
        // 连接断开时，所有在途 RPC 必须失败，否则删除/切换等按钮会永远挂起。
        for (const pending of this.pending.values()) {
          pending.reject(new Error("后端连接已断开"));
        }
        this.pending.clear();
        this.scheduleReconnect(url);
      };
      this.ws = ws;
    });
    return this.connectPromise;
  }

  private scheduleReconnect(url: string): void {
    if (this.reconnectTimer) return;
    const delay = Math.min(1000 * 2 ** this.attempts, MAX_BACKOFF_MS);
    this.attempts += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect(url).catch(() => {
        // 首次失败已由 onclose 的 scheduleReconnect 驱动重试；这里吞掉即可。
      });
    }, delay);
  }

  private handleMessage(raw: string): void {
    let msg: ResponseEnvelope | EventEnvelope;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    if (typeof (msg as ResponseEnvelope).request_id === "number") {
      const envelope = msg as ResponseEnvelope;
      const pending = this.pending.get(envelope.request_id);
      if (!pending) return;
      this.pending.delete(envelope.request_id);
      if (envelope.error) {
        pending.reject(new Error(envelope.error.message || "backend error"));
      } else {
        pending.resolve(envelope.result);
      }
      return;
    }

    if (typeof (msg as EventEnvelope).kind === "string") {
      const envelope = msg as EventEnvelope;
      for (const handler of this.handlers) {
        handler(envelope.kind, envelope.data);
      }
    }
  }

  async call(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    const ws = await this.connect();
    if (ws.readyState !== WebSocket.OPEN) {
      throw new Error("后端未连接");
    }
    const requestId = this.nextId++;
    const promise = new Promise<unknown>((resolve, reject) => {
      this.pending.set(requestId, { resolve, reject });
    });
    ws.send(JSON.stringify({ request_id: requestId, method, params }));
    return promise;
  }

  subscribe(handler: EventHandler): () => void {
    this.handlers.add(handler);
    return () => {
      this.handlers.delete(handler);
    };
  }
}

export const wsBackend = new WsBackend();
