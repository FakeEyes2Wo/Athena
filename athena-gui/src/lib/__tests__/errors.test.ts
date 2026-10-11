import { describe, expect, it } from "vitest";
import { BackendError } from "../rpc-error";
import { errorMessage } from "../errors";

describe("errorMessage", () => {
  it("passes plain errors and values through unchanged", () => {
    expect(errorMessage(new Error("nothing to resume"))).toBe("nothing to resume");
    expect(errorMessage("boom")).toBe("boom");
  });

  it("explains known backend error codes in Chinese", () => {
    const err = new BackendError({ message: "cancel the active draft before starting another task", data: { code: "different_task" } });
    expect(errorMessage(err)).toContain("新建会话");
  });

  it("keeps the backend message for unknown codes", () => {
    const err = new BackendError({ message: "disk full", data: { code: "artifact_write_failed_x" } });
    expect(errorMessage(err)).toBe("disk full");
  });
});
