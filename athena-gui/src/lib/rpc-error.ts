export interface RpcErrorData {
  code?: string;
  retryable?: boolean;
  current_revision?: number;
}

export interface RpcErrorPayload {
  code?: number | string;
  message?: string;
  data?: RpcErrorData;
}

export class BackendError extends Error {
  readonly code?: string;
  readonly retryable?: boolean;
  readonly current_revision?: number;

  constructor(payload: RpcErrorPayload) {
    super(payload.message || "backend error");
    this.name = "BackendError";
    this.code = payload.data?.code
      ?? (typeof payload.code === "string" ? payload.code : undefined);
    this.retryable = payload.data?.retryable;
    this.current_revision = payload.data?.current_revision;
  }
}

function payloadFrom(error: unknown): RpcErrorPayload | null {
  if (typeof error === "string") {
    try {
      return JSON.parse(error) as RpcErrorPayload;
    } catch {
      return { message: error };
    }
  }
  if (error && typeof error === "object" && "message" in error) {
    return error as RpcErrorPayload;
  }
  return null;
}

export function toBackendError(error: unknown): Error {
  if (error instanceof Error) return error;
  const payload = payloadFrom(error);
  return payload ? new BackendError(payload) : new Error("backend error");
}
