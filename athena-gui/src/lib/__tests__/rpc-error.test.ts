import { describe, expect, it } from "vitest";

import { BackendError, toBackendError } from "../rpc-error";

describe("toBackendError", () => {
  it("preserves a structured Tauri domain error", () => {
    const error = toBackendError({
      code: -32602,
      message: "stale revision",
      data: {
        code: "stale_revision",
        retryable: true,
        current_revision: 7,
      },
    }) as BackendError;

    expect(error.code).toBe("stale_revision");
    expect(error.retryable).toBe(true);
    expect(error.current_revision).toBe(7);
  });

  it("parses the same payload from a legacy JSON string", () => {
    const error = toBackendError(JSON.stringify({
      code: -32602,
      message: "stale revision",
      data: { code: "stale_revision" },
    })) as BackendError;

    expect(error.code).toBe("stale_revision");
  });
});
