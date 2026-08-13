import { describe, expect, it } from "vitest"
import { AthenaValidationError } from "../src/errors.js"

describe("AthenaValidationError", () => {
  it("carries name and message", () => {
    const err = new AthenaValidationError("boom")
    expect(err.name).toBe("AthenaValidationError")
    expect(err.message).toBe("boom")
    expect(err).toBeInstanceOf(Error)
  })
})
