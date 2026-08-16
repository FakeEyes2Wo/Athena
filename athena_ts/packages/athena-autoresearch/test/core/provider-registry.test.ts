import { describe, expect, it } from "vitest"
import { ProviderRegistry } from "../../src/index.js"

const provider = { id: "p1", version: "1", capabilities: ["a", "b"] }

describe("ProviderRegistry", () => {
  it("registers and retrieves providers", () => {
    const registry = new ProviderRegistry()
    registry.register(provider)
    expect(registry.get("p1")).toBe(provider)
    expect(registry.has("p1")).toBe(true)
  })

  it("rejects duplicate provider ids", () => {
    const registry = new ProviderRegistry()
    registry.register(provider)
    expect(() => registry.register(provider)).toThrow(/duplicate provider/)
  })

  it("throws on unknown provider", () => {
    const registry = new ProviderRegistry()
    expect(() => registry.get("missing")).toThrow(/unknown provider/)
  })

  it("filters by capability", () => {
    const registry = new ProviderRegistry()
    registry.register(provider)
    registry.register({ id: "p2", version: "1", capabilities: ["b"] })
    expect(registry.list("a").map((p) => p.id)).toEqual(["p1"])
    expect(registry.list("b").map((p) => p.id)).toEqual(["p1", "p2"])
  })
})
