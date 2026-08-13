import { describe, expect, it } from "vitest"
import { newId } from "../src/id.js"

describe("newId", () => {
  it("prepends prefix and 12 hex chars", () => {
    const id = newId("run")
    expect(id.startsWith("run_")).toBe(true)
    expect(id.slice(4)).toMatch(/^[0-9a-f]{12}$/)
  })
  it("rejects invalid prefixes", () => {
    expect(() => newId("")).toThrow(/alphanumeric/)
    expect(() => newId("bad prefix")).toThrow(/alphanumeric/)
    expect(() => newId("bad-prefix")).toThrow(/alphanumeric/)
  })
})
