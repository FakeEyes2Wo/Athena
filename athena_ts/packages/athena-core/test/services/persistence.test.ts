import fs from "node:fs"
import { mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import { atomicWriteJson } from "../../src/services/persistence.js"

describe("atomicWriteJson", () => {
  afterEach(() => { vi.restoreAllMocks() })
  it("writes indent-2 utf8 json with trailing newline", () => {
    const temp = mkdtempSync(join(tmpdir(), "athena-persist-"))
    const target = join(temp, "nested", "state.json")
    atomicWriteJson(target, { a: 1, 中文: "值" })
    const text = readFileSync(target, "utf-8")
    expect(text).toContain('  "a": 1')
    expect(text).toContain('"中文": "值"')
    expect(text.endsWith("\n")).toBe(true)
  })
  it("failed replace keeps last valid file", () => {
    const temp = mkdtempSync(join(tmpdir(), "athena-persist-"))
    const target = join(temp, "research_tree.json")
    writeFileSync(target, '{"last":"valid"}', "utf-8")
    vi.spyOn(fs, "renameSync").mockImplementation(() => { throw new Error("disk full") })
    expect(() => atomicWriteJson(target, { new: true })).toThrow(/disk full/)
    expect(readFileSync(target, "utf-8")).toBe('{"last":"valid"}')
    expect(readdirSync(temp)).toEqual(["research_tree.json"])
  })
})
